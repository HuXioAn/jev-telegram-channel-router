"""频道级执行管线：抓新消息 → 联合判定（缓存）→ 按订阅路由投递。

- run_watch()：一个频道的一轮刷新（调度单位=频道；见 PLAN §12）；
- preview()：/test 试跑（按单订阅模板独立判定，不写共享缓存）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import formatting
from .channel_fetch import ChannelError, ChannelFetcher
from .delivery import DeliveryError, Sender
from .jev import JevClient
from .judging import JudgeEngine, template_fingerprint
from .models import Post, Template
from .store import Store, month_start, template_of


@dataclass
class RunResult:
    sub_id: int | None = None       # 试跑：订阅编号
    channel: str = ""               # 频道轮次：频道名
    fetched: int = 0
    matched: int = 0
    judged: int = 0                 # 本轮新判定的消息数
    cached: int = 0                 # 命中判定缓存的消息数
    failed: int = 0
    sent: bool = False
    sample: list[tuple[str, Post, dict]] = field(default_factory=list)  # 试跑样张
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class Pipeline:
    def __init__(self, store: Store, fetcher: ChannelFetcher, jev: JevClient,
                 sender: Sender, chunk_limit: int = 3800,
                 max_questions: int = 24):
        self._store = store
        self._fetcher = fetcher
        self._jev = jev
        self._sender = sender
        self._chunk_limit = chunk_limit
        self._judge = JudgeEngine(store, jev, max_questions)

    # ------------------------------------------------------------- 配额
    def _quota_remaining(self, user_id: int) -> int | None:
        """本月剩余判定消费额度（按用户消费的判定消息条数计）；None = 不限。"""
        user = self._store.get_user(user_id) or {}
        quota = int(user.get("quota_jev_monthly") or 0)
        if quota <= 0:
            return None
        used = self._store.usage_sum(user_id=user_id, kind="consumed",
                                     since=month_start(datetime.now(timezone.utc)))
        return max(0, quota - used)

    def _pause_quota(self, user_id: int) -> list[int]:
        """配额用尽：暂停该用户全部启用中的订阅；返回被暂停的编号。"""
        paused = []
        for sub in self._store.list_subscriptions(user_id=user_id):
            if not sub["enabled"]:
                continue
            self._store.set_subscription(sub["id"], enabled=0)
            self._store.log(sub["id"], "quota_exhausted",
                            "本月判定配额已用完，订阅已自动暂停")
            paused.append(sub["id"])
        return paused

    # --------------------------------------------------------- 频道轮次
    async def run_watch(self, watch: dict) -> RunResult:
        """一个频道的一轮：抓新消息 → 联合判定一次 → 路由到全部 watcher 订阅。"""
        channel = watch["channel"]
        res = RunResult(channel=channel)
        watchers = self._store.watchers_of(channel)
        if not watchers:
            return res
        if watch["last_seen_id"] is None:  # 首次物化：用频道头部播种抓取游标
            try:
                info = await self._fetcher.head(channel)
            except ChannelError as exc:
                res.error = str(exc)
                self._store.mark_watch_fetched(channel)
                return res
            self._store.mark_watch_fetched(channel, info.head_id)
            return res
        try:
            posts, cursor = await self._fetcher.fetch_since(channel, watch["last_seen_id"])
        except ChannelError as exc:
            res.error = str(exc)
            self._store.mark_watch_fetched(channel)
            return res
        self._store.record_usage(0, "fetch", 1, detail=channel)
        posts = [p for p in posts if p.text]
        res.fetched = len(posts)
        if not posts:
            self._store.mark_watch_fetched(channel, cursor)
            return res

        # ---- 联合判定：该频道全部活跃模板的问题并集，一次调用服务所有订阅
        templates: dict[str, Template] = {}
        for sub in watchers:
            template = template_of(sub)
            templates.setdefault(template_fingerprint(template), template)
        judged, stats = await self._judge.ensure(channel, posts, templates)
        res.judged, res.cached, res.failed = stats.fresh, stats.cached, stats.failed
        if stats.calls:
            self._store.record_usage(
                0, "jev", stats.calls, detail=f"channel:{channel}",
                input_tokens=stats.input_tokens, output_tokens=stats.output_tokens)
        if stats.failed:
            self._store.log(None, "classify_failed",
                            f"@{channel}: {stats.failed} 条判定失败已跳过")

        # ---- 路由：每个订阅按自己的消费游标取新消息 → 求值 → 投递
        user_posts: dict[int, set[int]] = {}
        for sub in watchers:
            source_row = next((s for s in sub["sources"] if s["source"] == channel), None)
            if source_row is None:
                continue
            cursor_sub = source_row["last_seen_id"]
            mine = [p for p in posts if cursor_sub is None or p.id > cursor_sub]
            if not mine:
                continue
            user_posts.setdefault(sub["user_id"], set()).update(p.id for p in mine)
            template = template_of(sub)
            fp = template_fingerprint(template)
            hits = [(p, answers) for p in mine
                    if (answers := (judged.get(p.id) or {}).get(fp)) is not None
                    and template.evaluate(answers)]
            if hits:
                res.matched += len(hits)
                await self._deliver(sub, channel, hits, res, test=False)
            self._store.mark_source_run(source_row["id"], max(p.id for p in mine))

        # ---- 消费记账 + 配额（按用户、跨订阅去重）
        for user_id, post_ids in user_posts.items():
            self._store.record_usage(user_id, "consumed", len(post_ids),
                                     detail=channel)
            if self._quota_remaining(user_id) == 0:
                paused = self._pause_quota(user_id)
                if paused:
                    res.error = res.error or (
                        f"用户 {user_id} 判定配额用尽，已自动暂停订阅 {paused}")
        self._store.mark_watch_fetched(channel, cursor)  # 路由完成后推进频道游标
        return res

    # ------------------------------------------------------------- 投递
    async def _deliver(self, sub: dict, source: str,
                       hits: list[tuple[Post, dict]], res: RunResult,
                       *, test: bool) -> None:
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

    # ------------------------------------------------------------- 试跑
    async def _classify_batch(self, posts: list[Post], template: Template,
                              res: RunResult
                              ) -> tuple[list[tuple[Post, dict]], int, int, int]:
        """按单模板判定一批消息（试跑用）；返回 (hits, 成功判定数, in tok, out tok)。"""
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

    async def preview(self, sub: dict, pool: int = 120, limit: int = 6) -> RunResult:
        """试跑：逐源拉最近 pool 条样本判定；样张（每源最新 limit 条）发往全部目的地；
        不推进游标、不写共享缓存；仍计入用量与配额。"""
        res = RunResult(sub_id=sub["id"])
        template = template_of(sub)
        user_id = sub["user_id"]
        remaining = self._quota_remaining(user_id)
        if remaining is not None and remaining <= 0:
            res.error = "本月判定配额已用完（试跑同样计入配额）。"
            return res
        for src in sub["sources"]:
            source = src["source"]
            try:
                info = await self._fetcher.head(source)
                posts, _ = await self._fetcher.fetch_since(
                    source, max(1, info.head_id - pool))
            except ChannelError as exc:
                res.error = res.error or str(exc)
                continue
            self._store.record_usage(user_id, "fetch", 1, sub_id=sub["id"],
                                     detail=source)
            posts = [p for p in posts if p.text][-pool:]
            if remaining is not None:
                posts = posts[-remaining:]
            res.fetched += len(posts)
            if not posts:
                continue
            hits, judged, tin, tout = await self._classify_batch(posts, template, res)
            if judged:
                self._store.record_usage(user_id, "jev", judged, sub_id=sub["id"],
                                         input_tokens=tin, output_tokens=tout,
                                         detail=source)
                self._store.record_usage(user_id, "consumed", judged,
                                         detail=f"preview:{source}")
            if remaining is not None:
                remaining = max(0, remaining - judged)
            sample_hits = hits[-limit:]  # 该源最新 limit 条，保持时间顺序
            res.sample.extend((source, post, answers)
                              for post, answers in reversed(sample_hits))
            if sample_hits:
                await self._deliver(sub, source, sample_hits, res, test=True)
        return res
