"""单订阅执行管线：续抓 → Jev 分类 → 规则匹配 → 合成 → 投递。"""
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
    sample: list[tuple[Post, dict]] = field(default_factory=list)
    error: str = ""


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
                                   res: RunResult) -> list[tuple[Post, dict]]:
        results = await self._jev.classify_many([p.text for p in posts], template)
        hits: list[tuple[Post, dict]] = []
        for post, result in zip(posts, results):
            if result.get("error"):
                res.failed += 1
                continue
            if template.evaluate(result["answers"]):
                hits.append((post, result["answers"]))
        res.matched = len(hits)
        return hits

    async def run(self, sub: dict, *, dry: bool = False) -> RunResult:
        """执行一次订阅：抓新消息 → 判定 → 命中则投递 → 推进游标。"""
        res = RunResult(sub_id=sub["id"])
        template = template_of(sub)
        user_id = sub["user_id"]
        after = sub["last_seen_id"]
        if not after:
            # 防御：没有游标时只把游标对齐到头部，不抓全量历史
            try:
                info = await self._fetcher.head(sub["source"])
            except ChannelError as exc:
                res.error = str(exc)
                return res
            if not dry:
                self._store.mark_run(sub["id"], info.head_id)
            return res

        remaining = self._quota_remaining(user_id)
        if remaining is not None and remaining <= 0:
            if not dry:
                self._pause_quota(sub)
            res.error = "本月 Jev 判定配额已用完，订阅已自动暂停；请联系管理员调整配额。"
            return res
        if not dry:
            self._store.record_usage(user_id, "run", 1, sub_id=sub["id"], detail="run")

        try:
            posts, cursor = await self._fetcher.fetch_since(sub["source"], after)
        except ChannelError as exc:
            res.error = str(exc)
            if not dry:
                self._store.log(sub["id"], "fetch_error", str(exc))
            return res
        res.fetched = len(posts)
        if not dry:
            self._store.record_usage(user_id, "fetch", 1, sub_id=sub["id"])
        if not posts:
            if not dry:
                self._store.mark_run(sub["id"], after)
            return res

        used_posts = posts if remaining is None else posts[:remaining]
        hits = await self._classify_and_select(used_posts, template, res)
        if not dry:
            judged = max(0, len(used_posts) - res.failed)
            if judged:
                self._store.record_usage(user_id, "jev", judged, sub_id=sub["id"])
            if len(used_posts) < len(posts):
                self._store.log(sub["id"], "quota_truncated",
                                f"配额将尽，仅判定 {len(used_posts)}/{len(posts)} 条")

        if hits and not dry:
            chunks = formatting.compose_digest(
                sub["source"], sub["id"], hits, template, self._chunk_limit)
            try:
                await self._sender.send(sub["dest_chat_id"], chunks)
                res.sent = True
                self._store.record_usage(user_id, "deliver", len(chunks),
                                         sub_id=sub["id"], detail=f"{len(hits)} hits")
                self._store.log(sub["id"], "delivered",
                                f"{len(hits)} hits / {len(chunks)} msgs")
            except DeliveryError as exc:
                res.error = f"投递失败：{exc}"
                self._store.log(sub["id"], "delivery_error", str(exc))
        if not dry:
            if res.failed:
                self._store.log(sub["id"], "classify_failed", f"{res.failed} 条判定失败已跳过")
            left = self._quota_remaining(user_id)
            if left is not None and left <= 0:
                self._pause_quota(sub)
                res.error = res.error or "本月 Jev 判定配额已用完，订阅已自动暂停。"
            self._store.mark_run(sub["id"], cursor)
        return res

    async def preview(self, sub: dict, pool: int = 120, limit: int = 6) -> RunResult:
        """试跑：拉最近 pool 条样本判定；把样张（最新 limit 条）发到订阅目标；不推进游标。仍计入用量与配额。"""
        res = RunResult(sub_id=sub["id"])
        template = template_of(sub)
        user_id = sub["user_id"]
        remaining = self._quota_remaining(user_id)
        if remaining is not None and remaining <= 0:
            res.error = "本月 Jev 判定配额已用完（试跑同样计入配额）。"
            return res
        self._store.record_usage(user_id, "run", 1, sub_id=sub["id"], detail="preview")
        try:
            info = await self._fetcher.head(sub["source"])
            posts, _ = await self._fetcher.fetch_since(sub["source"],
                                                       max(1, info.head_id - pool))
        except ChannelError as exc:
            res.error = str(exc)
            return res
        self._store.record_usage(user_id, "fetch", 1, sub_id=sub["id"])
        posts = [p for p in posts if p.text][-pool:]
        if remaining is not None:
            posts = posts[-remaining:]
        res.fetched = len(posts)
        if not posts:
            return res
        hits = await self._classify_and_select(posts, template, res)
        judged = max(0, len(posts) - res.failed)
        if judged:
            self._store.record_usage(user_id, "jev", judged, sub_id=sub["id"])
        sample_hits = hits[-limit:]  # 最新 limit 条，保持时间顺序
        res.sample = list(reversed(sample_hits))  # DM 展示：最新在前
        if sample_hits:
            chunks = formatting.compose_digest(
                sub["source"], sub["id"], sample_hits, template,
                self._chunk_limit, test=True)
            try:
                await self._sender.send(sub["dest_chat_id"], chunks)
                res.sent = True
                self._store.record_usage(user_id, "deliver", len(chunks),
                                         sub_id=sub["id"], detail="preview")
            except DeliveryError as exc:
                res.error = f"样张发送到目标失败：{exc}"
        return res
