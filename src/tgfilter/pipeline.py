"""单订阅执行管线：续抓 → Jev 分类 → 规则匹配 → 合成 → 投递。"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import formatting
from .channel_fetch import ChannelError, ChannelFetcher
from .delivery import DeliveryError, Sender
from .jev import JevClient
from .models import Post, Template
from .store import Store, template_of


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
        try:
            posts, cursor = await self._fetcher.fetch_since(sub["source"], after)
        except ChannelError as exc:
            res.error = str(exc)
            if not dry:
                self._store.log(sub["id"], "fetch_error", str(exc))
            return res
        res.fetched = len(posts)
        if not posts:
            if not dry:
                self._store.mark_run(sub["id"], after)
            return res

        hits = await self._classify_and_select(posts, template, res)

        if hits and not dry:
            chunks = formatting.compose_digest(
                sub["source"], sub["id"], hits, template, self._chunk_limit)
            try:
                await self._sender.send(sub["dest_chat_id"], chunks)
                res.sent = True
                self._store.log(sub["id"], "delivered",
                                f"{len(hits)} hits / {len(chunks)} msgs")
            except DeliveryError as exc:
                res.error = f"投递失败：{exc}"
                self._store.log(sub["id"], "delivery_error", str(exc))
        if not dry:
            if res.failed:
                self._store.log(sub["id"], "classify_failed", f"{res.failed} 条判定失败已跳过")
            self._store.mark_run(sub["id"], cursor)
        return res

    async def preview(self, sub: dict, pool: int = 120, limit: int = 6) -> RunResult:
        """试跑：拉最近 pool 条样本判定；不发送、不推进游标。"""
        res = RunResult(sub_id=sub["id"])
        template = template_of(sub)
        try:
            info = await self._fetcher.head(sub["source"])
            posts, _ = await self._fetcher.fetch_since(sub["source"],
                                                       max(1, info.head_id - pool))
        except ChannelError as exc:
            res.error = str(exc)
            return res
        posts = [p for p in posts if p.text][-pool:]
        res.fetched = len(posts)
        if not posts:
            return res
        hits = await self._classify_and_select(posts, template, res)
        res.sample = list(reversed(hits[-limit:]))  # 最新在前
        return res
