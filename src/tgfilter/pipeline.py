"""Channel-level execution pipeline: fetch new posts → union judging (cached) → route per subscription.

- run_watch(): one refresh round for a channel (the scheduler unit; see PLAN §12);
- preview(): /test dry-run (judges with the single subscription template and never
  writes the shared judgment cache).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import formatting
from .channel_fetch import ChannelError, ChannelFetcher
from .delivery import DeliveryError, Sender
from .i18n import t
from .jev import JevClient
from .judging import JudgeEngine, template_fingerprint
from .models import Post, Template
from .store import Store, month_start, template_of


@dataclass
class RunResult:
    sub_id: int | None = None       # dry-run: subscription id
    channel: str = ""               # channel round: channel name
    fetched: int = 0
    matched: int = 0
    judged: int = 0                 # posts judged fresh in this round
    cached: int = 0                 # posts served from the judgment cache
    failed: int = 0
    sent: bool = False
    sample: list[tuple[str, Post, dict]] = field(default_factory=list)  # dry-run sample
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class Pipeline:
    def __init__(self, store: Store, fetcher: ChannelFetcher, jev: JevClient,
                 sender: Sender, chunk_limit: int = 3800,
                 max_questions: int = 24, default_lang: str = "en"):
        self._store = store
        self._fetcher = fetcher
        self._jev = jev
        self._sender = sender
        self._chunk_limit = chunk_limit
        self._default_lang = default_lang
        self._judge = JudgeEngine(store, jev, max_questions)

    def _lang(self, user_id: int) -> str:
        """UI language for user-facing text produced by the pipeline."""
        return self._store.language_for(user_id, self._default_lang)

    # ---------------------------------------------------------------- quota
    def _quota_remaining(self, user_id: int) -> int | None:
        """Judgments left this month (counted per consumed post); None = unlimited."""
        user = self._store.get_user(user_id) or {}
        quota = int(user.get("quota_jev_monthly") or 0)
        if quota <= 0:
            return None
        used = self._store.usage_sum(user_id=user_id, kind="consumed",
                                     since=month_start(datetime.now(timezone.utc)))
        return max(0, quota - used)

    def _pause_quota(self, user_id: int) -> list[int]:
        """Quota exhausted: pause every enabled subscription; returns the paused ids."""
        paused = []
        for sub in self._store.list_subscriptions(user_id=user_id):
            if not sub["enabled"]:
                continue
            self._store.set_subscription(sub["id"], enabled=0)
            self._store.log(sub["id"], "quota_exhausted",
                            "monthly judgment quota exhausted; subscription auto-paused")
            paused.append(sub["id"])
        return paused

    # --------------------------------------------------------- channel round
    async def run_watch(self, watch: dict) -> RunResult:
        """One channel round: fetch new posts → judge once (union) → route to every watcher."""
        channel = watch["channel"]
        res = RunResult(channel=channel)
        watchers = self._store.watchers_of(channel)
        if not watchers:
            return res
        if watch["last_seen_id"] is None:  # first materialization: seed from the channel head
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

        # ---- Union judging: one call serves every subscription on this channel
        # (question set = union of all active templates, deduplicated)
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
                            f"@{channel}: {stats.failed} posts failed judging (skipped)")

        # ---- Routing: each subscription reads from its own consumption cursor → evaluate → deliver
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
                await self._deliver(sub, channel, hits, res, test=False,
                                    lang=self._lang(sub["user_id"]))
            self._store.mark_source_run(source_row["id"], max(p.id for p in mine))

        # ---- Consumption accounting + quota (per user, deduplicated across subscriptions)
        for user_id, post_ids in user_posts.items():
            self._store.record_usage(user_id, "consumed", len(post_ids),
                                     detail=channel)
            if self._quota_remaining(user_id) == 0:
                paused = self._pause_quota(user_id)
                if paused:
                    res.error = res.error or t(self._lang(user_id), "quota_paused",
                                               uid=user_id, paused=paused)
        self._store.mark_watch_fetched(channel, cursor)  # advance the channel cursor after routing
        return res

    # ------------------------------------------------------------- delivery
    async def _deliver(self, sub: dict, source: str,
                       hits: list[tuple[Post, dict]], res: RunResult,
                       *, test: bool, lang: str) -> None:
        """Send one source's hits to every target of the subscription (independent outcomes)."""
        chunks = formatting.compose_digest(hits, chunk_limit=self._chunk_limit,
                                           test=test, lang=lang)
        sent_any = False
        for dest in sub["dests"]:
            label = dest["title"] or str(dest["chat_id"])
            try:
                await self._sender.send(dest["chat_id"], chunks)
                sent_any = True
                self._store.record_usage(sub["user_id"], "deliver", len(chunks),
                                         sub_id=sub["id"], detail=label)
            except DeliveryError as exc:
                res.error = res.error or t(lang, "deliver_failed", label=label, err=exc)
                self._store.log(sub["id"], "delivery_error", f"{label}: {exc}")
        if sent_any:
            res.sent = True
            self._store.log(sub["id"], "delivered",
                            f"{source}: {len(hits)} hits / {len(chunks)} msgs")

    # ------------------------------------------------------------- dry-run
    async def _classify_batch(self, posts: list[Post], template: Template,
                              res: RunResult
                              ) -> tuple[list[tuple[Post, dict]], int, int, int]:
        """Judge one batch with a single template (dry-run); returns (hits, judged, in tok, out tok)."""
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
        """Dry-run: fetch the latest pool posts per source and judge them; the sample
        (latest limit hits per source) goes to every target; cursors are not advanced
        and the shared cache is not written; usage/quota still apply.
        """
        res = RunResult(sub_id=sub["id"])
        template = template_of(sub)
        user_id = sub["user_id"]
        lang = self._lang(user_id)
        remaining = self._quota_remaining(user_id)
        if remaining is not None and remaining <= 0:
            res.error = t(lang, "preview_quota_exhausted")
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
            sample_hits = hits[-limit:]  # latest limit hits, chronological order
            res.sample.extend((source, post, answers)
                              for post, answers in reversed(sample_hits))
            if sample_hits:
                await self._deliver(sub, source, sample_hits, res, test=True, lang=lang)
        return res

