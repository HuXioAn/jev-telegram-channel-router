"""单订阅执行管线：多源续抓 → Jev 分类 → 规则匹配 → 合成 → 多目的地投递。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import formatting
from .channel_fetch import ChannelError, ChannelFetcher
from .delivery import DeliveryError, Sender
from .jev import JevClient
from .models import Post, Template
from .store import Store, month_start, template_of


@dataclass
class RunResult:
    sub_id: int
    fetched: int = 0
    matched: int = 0
    failed: int = 0
    sent: bool = False
    sample: list[tuple[str, Post, dict]] = field(default_factory=list)  # (源, 消息, 判定)
    error: str = ""
    input_tokens: int = 0    # 本轮 Jev 判定累计（API 返回的真实用量）
    output_tokens: int = 0


class Pipeline:
    def __init__(self, store: Store, fetcher: ChannelFetcher, jev: JevClient,
                 sender: Sender, chunk_limit: int = 3800):
        self._store = store
        self._fetcher = fetcher
        self._jev = jev
        self._sender = sender
        self._chunk_limit = chunk_limit

    # ------------------------------------------------------------- 配额
    def _quota_remaining(self, user_id: int) -> int | None:
        """本月 Jev 判定剩余配额；None = 不限。"""
        user = self._store.get_user(user_id) or {}
        quota = int(user.get("quota_jev_monthly") or 0)
        if quota <= 0:
            return None
        used = self._store.usage_sum(user_id=user_id, kind="jev",
                                     since=month_start(datetime.now(timezone.utc)))
        return max(0, quota - used)

    def _pause_quota(self, sub: dict) -> None:
        self._store.set_subscription(sub["id"], enabled=0)
        self._store.log(sub["id"], "quota_exhausted", "本月 Jev 配额已用完，订阅已自动暂停")

    async def _classify_and_select(self, posts: list[Post], template: Template,
                                   res: RunResult
                                   ) -> tuple[list[tuple[Post, dict]], int, int, int]:
        """判定一批消息；返回 (hits, 成功判定数, 本批 input tokens, 本批 output tokens)。"""
        results = await self._jev.classify_many([p.text for p in posts], template)
        hits: list[tuple[Post, dict]] = []
        judged, tin, tout = 0, 0, 0
        for post, result in zip(posts, results):
            usage = result.get("usage") or {}
            tin += int(usage.get("input_tokens") or 0)
            tout += int(usage.get("output_tokens") or 0)
            if result.get("error"):
                res.failed += 1
                continue
            judged += 1
            if template.evaluate(result["answers"]):
                hits.append((post, result["answers"]))
        res.input_tokens += tin
        res.output_tokens += tout
        res.matched += len(hits)
        return hits, judged, tin, tout

    async def _deliver(self, sub: dict, source: str, hits: list[tuple[Post, dict]],
                       template: Template, res: RunResult, *, test: bool) -> None:
        """把某源频道的命中摘要发往订阅的全部目的地（各目的地独立成败）。"""
        chunks = formatting.compose_digest(hits, chunk_limit=self._chunk_limit,
                                           test=test)
        sent_any = False
        for dest in sub["dests"]:
            label = dest["title"] or str(dest["chat_id"])
            try:
                await self._sender.send(dest["chat_id"], chunks)
                sent_any = True
                self._store.record_usage(sub["user_id"], "deliver", len(chunks),
                                         sub_id=sub["id"], detail=label)
            except DeliveryError as exc:
                res.error = res.error or f"投递失败（{label}）：{exc}"
                self._store.log(sub["id"], "delivery_error", f"{label}: {exc}")
        if sent_any:
            res.sent = True
            self._store.log(sub["id"], "delivered",
                            f"{source}: {len(hits)} hits / {len(chunks)} msgs")

    async def run(self, sub: dict, *, dry: bool = False) -> RunResult:
        """执行一次订阅：逐源抓新消息 → 判定 → 命中发往全部目的地 → 推进该源游标。"""
        res = RunResult(sub_id=sub["id"])
        template = template_of(sub)
        user_id = sub["user_id"]
        remaining = self._quota_remaining(user_id)
        if remaining is not None and remaining <= 0:
            if not dry:
                self._pause_quota(sub)
            res.error = "本月 Jev 判定配额已用完，订阅已自动暂停；请联系管理员调整配额。"
            return res
        if not dry:
            self._store.record_usage(user_id, "run", 1, sub_id=sub["id"], detail="run")

        for src in sub["sources"]:
            source = src["source"]
            after = src["last_seen_id"]
            if not after:
                # 防御：没有游标时只把游标对齐到头部，不抓全量历史
                try:
                    info = await self._fetcher.head(source)
                except ChannelError as exc:
                    res.error = res.error or str(exc)
                    continue
                if not dry:
                    self._store.mark_source_run(src["id"], info.head_id)
                continue
            try:
                posts, cursor = await self._fetcher.fetch_since(source, after)
            except ChannelError as exc:
                res.error = res.error or str(exc)
                if not dry:
                    self._store.log(sub["id"], "fetch_error", f"{source}: {exc}")
                continue
            res.fetched += len(posts)
            if not dry:
                self._store.record_usage(user_id, "fetch", 1,
                                         sub_id=sub["id"], detail=source)
            if not posts:
                if not dry:
                    self._store.mark_source_run(src["id"], after)
                continue

            used_posts = posts if remaining is None else posts[:remaining]
            hits, judged, tin, tout = await self._classify_and_select(used_posts, template, res)
            if not dry:
                if judged:
                    self._store.record_usage(user_id, "jev", judged, sub_id=sub["id"],
                                             input_tokens=tin, output_tokens=tout,
                                             detail=source)
                if len(used_posts) < len(posts):
                    self._store.log(sub["id"], "quota_truncated",
                                    f"{source}: 配额将尽，仅判定 {len(used_posts)}/{len(posts)} 条")
            if remaining is not None:
                remaining = max(0, remaining - judged)
            if hits and not dry:
                await self._deliver(sub, source, hits, template, res, test=False)
            if not dry:
                self._store.mark_source_run(src["id"], cursor)

        if not dry:
            if res.failed:
                self._store.log(sub["id"], "classify_failed", f"{res.failed} 条判定失败已跳过")
            left = self._quota_remaining(user_id)
            if left is not None and left <= 0:
                self._pause_quota(sub)
                res.error = res.error or "本月 Jev 判定配额已用完，订阅已自动暂停。"
            self._store.mark_run(sub["id"])
        return res

    async def preview(self, sub: dict, pool: int = 120, limit: int = 6) -> RunResult:
        """试跑：逐源拉最近 pool 条样本判定；样张（每源最新 limit 条）发往全部目的地；不推进游标。仍计入用量与配额。"""
        res = RunResult(sub_id=sub["id"])
        template = template_of(sub)
        user_id = sub["user_id"]
        remaining = self._quota_remaining(user_id)
        if remaining is not None and remaining <= 0:
            res.error = "本月 Jev 判定配额已用完（试跑同样计入配额）。"
            return res
        self._store.record_usage(user_id, "run", 1, sub_id=sub["id"], detail="preview")
        for src in sub["sources"]:
            source = src["source"]
            try:
                info = await self._fetcher.head(source)
                posts, _ = await self._fetcher.fetch_since(
                    source, max(1, info.head_id - pool))
            except ChannelError as exc:
                res.error = res.error or str(exc)
                continue
            self._store.record_usage(user_id, "fetch", 1, sub_id=sub["id"], detail=source)
            posts = [p for p in posts if p.text][-pool:]
            if remaining is not None:
                posts = posts[-remaining:]
            res.fetched += len(posts)
            if not posts:
                continue
            hits, judged, tin, tout = await self._classify_and_select(posts, template, res)
            if judged:
                self._store.record_usage(user_id, "jev", judged, sub_id=sub["id"],
                                         input_tokens=tin, output_tokens=tout,
                                         detail=source)
            if remaining is not None:
                remaining = max(0, remaining - judged)
            sample_hits = hits[-limit:]  # 该源最新 limit 条，保持时间顺序
            res.sample.extend((source, post, answers)
                              for post, answers in reversed(sample_hits))
            if sample_hits:
                await self._deliver(sub, source, sample_hits, template, res, test=True)
        return res
