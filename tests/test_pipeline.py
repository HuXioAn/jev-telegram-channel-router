"""Channel round pipeline: union judging (one call serves several templates), routing, cursors, cache idempotency, quota, dry run."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_template
from tgfilter.channel_fetch import ChannelError, ChannelInfo
from tgfilter.delivery import DeliveryError
from tgfilter.models import Post, Template
from tgfilter.pipeline import Pipeline
from tgfilter.store import Store

CHINA = "是否与中国相关？"
CRYPTO = "是否与加密货币相关？"


def make_tpl(*, name="t", qid="china", instructions=CHINA, threshold=0.7) -> Template:
    return Template.model_validate({
        "name": name,
        "questions": {qid: {"type": "noul", "title": qid,
                            "instructions": instructions}},
        "match": {"logic": "all",
                  "conditions": [{"question": qid, "op": ">=", "value": threshold}]},
    })


class FakeJev:
    """Union judging stub: scores by (text, instructions); records the number of questions carried by each call."""

    def __init__(self, scores: dict[str, dict[str, float]] | None = None,
                 default: float = 0.1, errors: set[str] | None = None):
        self.scores = scores or {}
        self.default = default
        self.errors = errors or set()
        self.calls: list[tuple[str, int]] = []   # union judging calls (text, question count)
        self.local_calls: list[str] = []         # classify_many (dry run) calls
        self.usage = {"input_tokens": 40, "output_tokens": 5}

    def _answer(self, text: str, instructions: str) -> dict:
        return {"type": "noul",
                "noul": self.scores.get(text, {}).get(instructions, self.default)}

    async def classify_questions(self, text: str, questions: dict) -> dict:
        self.calls.append((text, len(questions)))
        if text in self.errors:
            return {"error": "boom", "usage": dict(self.usage)}
        return {"answers": {aid: self._answer(text, payload.get("instructions", ""))
                            for aid, payload in questions.items()},
                "usage": dict(self.usage)}

    async def classify_many(self, texts, template) -> list[dict]:
        out = []
        for text in texts:
            self.local_calls.append(text)
            if text in self.errors:
                out.append({"error": "boom", "usage": dict(self.usage)})
                continue
            out.append({"answers": {qid: self._answer(text, q.instructions)
                                    for qid, q in template.questions.items()},
                        "usage": dict(self.usage)})
        return out


class FakeFetcher:
    def __init__(self, data: dict[str, list[Post]] | None = None,
                 head: ChannelInfo | None = None,
                 fetch_errors: set[str] | None = None):
        self.data = data or {}
        self.head_info = head
        self.fetch_errors = fetch_errors or set()
        self.head_calls = 0
        self.fetch_calls: list[tuple[str, int]] = []

    async def head(self, channel: str) -> ChannelInfo:
        self.head_calls += 1
        if self.head_info is None:
            raise ChannelError("boom")
        return self.head_info

    async def fetch_since(self, channel: str, after_id: int):
        self.fetch_calls.append((channel, after_id))
        if channel in self.fetch_errors:
            raise ChannelError("boom")
        posts = [p for p in self.data.get(channel, []) if p.id > after_id]
        return posts, max((p.id for p in posts), default=after_id)


class FakeSender:
    def __init__(self, error: str | None = None):
        self.error = error
        self.sent: list[tuple[int, list[str]]] = []

    async def send(self, chat_id: int, chunks: list[str]) -> None:
        if self.error:
            raise DeliveryError(self.error)
        self.sent.append((chat_id, chunks))


def _sub(store: Store, user_id: int, chat_id: int, cursor: int | None = 100,
         source: str = "chan", template=None) -> int:
    return store.add_subscription(
        user_id=user_id, template=template or make_template(),
        sources=[{"source": source, "last_seen_id": cursor}],
        dests=[{"kind": "dm", "chat_id": chat_id, "title": "私聊"}])


def _cursor(store: Store, sub_id: int) -> int | None:
    return store.get_subscription(sub_id)["sources"][0]["last_seen_id"]


# ---------------------------------------------------------------- basic rounds
async def test_run_watch_sends_hits_and_advances(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42)
    store.sync_watches()
    watch = store.get_watch("chan")
    assert watch["last_seen_id"] == 100
    posts = [Post(id=101, text="hello", url="u1"), Post(id=102, text="world", url="u2")]
    jev = FakeJev({"hello": {CHINA: 0.95}})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)

    result = await pipeline.run_watch(watch)

    assert (result.fetched, result.matched, result.sent) == (2, 1, True)
    assert sender.sent[0][0] == 42 and "hello" in sender.sent[0][1][0]
    assert "u1" in sender.sent[0][1][0]
    assert len(jev.calls) == 2                       # one union judging call per message
    assert _cursor(store, sub_id) == 102
    watch = store.get_watch("chan")
    assert watch["last_seen_id"] == 102 and watch["last_fetch_at"]
    # Accounting: channel-level fetch/jev (real tokens are shared per channel), user-level consumed/deliver
    assert store.usage_by_kind(user_id=0) == {"fetch": 1, "jev": 2}
    assert store.usage_rollup(user_id=0)["jev"] == {"count": 2, "in": 80, "out": 10}
    assert store.usage_by_kind(user_id=7) == {"consumed": 2, "deliver": 1}


async def test_run_watch_two_templates_one_call_per_post(tmp_path):
    """Two different templates on the same channel (different users): each message triggers one Jev call only, and results are routed per template."""
    store = Store(str(tmp_path / "t.db"))
    a = _sub(store, 7, 42, template=make_tpl(name="中国"))
    b = _sub(store, 8, 43, template=make_tpl(name="加密", qid="crypto",
                                             instructions=CRYPTO))
    store.sync_watches()
    posts = [Post(id=101, text="hello", url="u1"), Post(id=102, text="world", url="u2")]
    jev = FakeJev({"hello": {CHINA: 0.95, CRYPTO: 0.1},
                   "world": {CHINA: 0.1, CRYPTO: 0.9}})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert len(jev.calls) == 2                       # 2 messages × 2 templates → still only 2 calls
    assert all(n == 2 for _, n in jev.calls)         # each call carries the union of both templates' questions
    assert (result.fetched, result.matched) == (2, 2)
    assert [(chat, "hello" in chunks[0], "world" in chunks[0])
            for chat, chunks in sender.sent] == [(42, True, False), (43, False, True)]
    assert _cursor(store, a) == 102 and _cursor(store, b) == 102
    assert store.usage_by_kind(user_id=7) == {"consumed": 2, "deliver": 1}
    assert store.usage_by_kind(user_id=8) == {"consumed": 2, "deliver": 1}


async def test_run_watch_question_dedup_across_templates(tmp_path):
    """Question-level dedup: two templates with identical question payloads (only the threshold differs) → the union asks once."""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42, template=make_tpl(name="严", threshold=0.7))
    _sub(store, 8, 43, template=make_tpl(name="宽", threshold=0.5))
    store.sync_watches()
    posts = [Post(id=101, text="hello", url="u1")]
    jev = FakeJev({"hello": {CHINA: 0.6}})
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, FakeSender())

    await pipeline.run_watch(store.get_watch("chan"))

    assert jev.calls == [("hello", 1)]               # the union holds a single question


async def test_run_watch_template_edit_affects_new_posts(tmp_path):
    """Template edit (question set changes → new fingerprint): new messages are judged and routed with the new template."""
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42)
    store.sync_watches()
    posts = [Post(id=101, text="a", url="u1")]
    jev = FakeJev({"a": {CHINA: 0.9}})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)
    await pipeline.run_watch(store.get_watch("chan"))
    assert sender.sent and "a" in sender.sent[0][1][0]

    store.set_subscription(sub_id, template_json=make_tpl(name="加密", qid="crypto",
                                                          instructions=CRYPTO
                                                          ).model_dump_json())
    store.mark_watch_fetched("chan", 100)            # simulate a rollback and refetch
    posts.append(Post(id=102, text="b", url="u2"))
    jev.scores["b"] = {CRYPTO: 0.9}
    await pipeline.run_watch(store.get_watch("chan"))
    # Only the new message b was judged (a is already cached and the new fingerprint does not apply to the old messages — the cursor has passed them, so it does not matter)
    assert ("b", 1) in jev.calls
    assert sender.sent[-1][0] == 42 and "b" in sender.sent[-1][1][0]


# ---------------------------------------------------------------- time decoupling
async def test_run_watch_laggard_sub_gets_backlog(tmp_path):
    """Time decoupling: subscriptions on the same channel have different cursors → each backfills only the messages it is missing, independently."""
    store = Store(str(tmp_path / "b.db"))
    a = _sub(store, 7, 42, cursor=100)
    b = _sub(store, 8, 43, cursor=200)
    store.sync_watches()
    assert store.get_watch("chan")["last_seen_id"] == 100   # materialization takes the minimum subscription cursor
    posts = [Post(id=i, text=f"m{i}", url=f"u{i}") for i in range(195, 206)]
    jev = FakeJev({f"m{i}": {CHINA: 0.9} for i in range(195, 206)})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)

    await pipeline.run_watch(store.get_watch("chan"))

    assert len(jev.calls) == 11                      # one call per message
    assert _cursor(store, a) == 205 and _cursor(store, b) == 205
    first = [messages[0] for chat, messages in sender.sent if chat == 42]
    second = [messages[0] for chat, messages in sender.sent if chat == 43]
    assert len(first) == 11 and len(second) == 5
    assert all(f"m{i}" in message for i, message in enumerate(first, 195))
    assert all("\n\n" not in message for message in first)
    assert "m201" in second[0] and "m195" not in second[0]
    assert "m205" in second[-1]
    assert store.usage_by_kind(user_id=7)["consumed"] == 11
    assert store.usage_by_kind(user_id=8)["consumed"] == 5
    assert store.usage_by_kind(user_id=7)["deliver"] == 11
    assert store.usage_by_kind(user_id=8)["deliver"] == 5


async def test_run_watch_rerun_is_free_and_idempotent(tmp_path):
    """Judgment cache + consumption cursor: rerunning after a fetch-cursor rollback calls neither Jev again nor delivers again."""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42)
    store.sync_watches()
    posts = [Post(id=101, text="hello", url="u1")]
    jev = FakeJev({"hello": {CHINA: 0.9}})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)
    await pipeline.run_watch(store.get_watch("chan"))

    store.mark_watch_fetched("chan", 100)            # roll the fetch cursor back
    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.cached == 1 and result.judged == 0
    assert len(jev.calls) == 1                       # no duplicate call
    assert len(sender.sent) == 1                     # no duplicate delivery
    assert store.usage_by_kind(user_id=7) == {"consumed": 1, "deliver": 1}


async def test_run_watch_no_new_posts(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42)
    store.sync_watches()
    jev = FakeJev({})
    pipeline = Pipeline(store, FakeFetcher({"chan": []}), jev, FakeSender())

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.fetched == 0 and jev.calls == []
    watch = store.get_watch("chan")
    assert watch["last_fetch_at"] and watch["last_seen_id"] == 100


async def test_run_watch_seeds_cursor_from_head(tmp_path):
    """A newly created channel has no cursor when first materialized: seed the head only, do not fetch history."""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42, cursor=None)
    store.sync_watches()
    assert store.get_watch("chan")["last_seen_id"] is None
    head = ChannelInfo(channel="chan", title="t", head_id=555, posts=[])
    fetcher = FakeFetcher(head=head)
    jev = FakeJev({})
    pipeline = Pipeline(store, fetcher, jev, FakeSender())

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.error == "" and fetcher.fetch_calls == []
    assert fetcher.head_calls == 1
    assert store.get_watch("chan")["last_seen_id"] == 555


# ---------------------------------------------------------------- errors and quota
async def test_run_watch_fetch_error_marks_fetched(tmp_path):
    """Fetch failure: log the error, update last_fetch_at (so it is not retried every minute), leave the cursor alone."""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42)
    store.sync_watches()
    fetcher = FakeFetcher(fetch_errors={"chan"})
    pipeline = Pipeline(store, fetcher, FakeJev({}), FakeSender())

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert "boom" in result.error
    watch = store.get_watch("chan")
    assert watch["last_fetch_at"] and watch["last_seen_id"] == 100


async def test_run_watch_classify_failure_skips_but_advances(tmp_path):
    posts = [Post(id=101, text="a", url="u1"), Post(id=102, text="b", url="u2")]
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42)
    store.sync_watches()
    jev = FakeJev({"b": {CHINA: 0.1}}, errors={"a"})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.failed == 1 and result.matched == 0 and sender.sent == []
    assert len(jev.calls) == 2
    assert _cursor(store, sub_id) == 102             # failed messages are skipped, the cursor advances as usual
    assert store.get_watch("chan")["last_seen_id"] == 102
    logs = [row["kind"] for row in store._query("SELECT kind FROM logs")]
    assert "classify_failed" in logs


async def test_run_watch_multi_dest_partial_failure(tmp_path):
    class FlakySender(FakeSender):
        async def send(self, chat_id: int, chunks: list[str]) -> None:
            if chat_id == -1005:
                raise DeliveryError("forbidden")
            self.sent.append((chat_id, chunks))

    store = Store(str(tmp_path / "t.db"))
    store.add_subscription(
        user_id=7, template=make_template(),
        sources=[{"source": "chan", "last_seen_id": 100}],
        dests=[{"kind": "dm", "chat_id": 42, "title": "私聊"},
               {"kind": "channel", "chat_id": -1005, "title": "测试频道"}])
    store.sync_watches()
    posts = [Post(id=101, text="hello", url="u1")]
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}),
                        FakeJev({"hello": {CHINA: 0.9}}), FlakySender(),
                        default_lang="zh")

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.sent is True
    assert "投递失败（测试频道）" in result.error
    logs = [row["kind"] for row in store._query("SELECT kind FROM logs")]
    assert "delivery_error" in logs and "delivered" in logs


async def test_run_watch_quota_exhausted_pauses_after_batch(tmp_path):
    """Quota exhausted: once the batch has finished routing, all of that user's subscriptions are paused automatically."""
    store = Store(str(tmp_path / "t.db"))
    store.add_user(7)
    sub_id = _sub(store, 7, 42)
    store.sync_watches()
    store.record_usage(7, "consumed", 2)
    store.set_user_fields(7, quota_jev_monthly=3)
    posts = [Post(id=101, text="a", url="u1"), Post(id=102, text="b", url="u2")]
    jev = FakeJev({"a": {CHINA: 0.9}, "b": {CHINA: 0.9}})
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, FakeSender(),
                        default_lang="zh")

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert "配额" in result.error
    assert store.get_subscription(sub_id)["enabled"] == 0
    assert store.usage_sum(user_id=7, kind="consumed") == 4   # 2 + 2 from this batch
    logs = [row["kind"] for row in store._query("SELECT kind FROM logs")]
    assert "quota_exhausted" in logs
    # Later rounds: only paused subscriptions remain on that channel → treated as having no watchers, no judging and no consumption
    jev.calls.clear()
    store.mark_watch_fetched("chan", 100)
    posts.append(Post(id=103, text="c", url="u3"))
    jev.scores["c"] = {CHINA: 0.9}
    await pipeline.run_watch(store.get_watch("chan"))
    assert jev.calls == []
    assert store.usage_sum(user_id=7, kind="consumed") == 4


# ---------------------------------------------------------------- dry run
async def test_preview_sends_sample_to_destination(tmp_path):
    """Dry run: the sample (newest `limit` messages, with the 🧪 header) is sent to the subscription destination; the cursor is not advanced."""
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42, cursor=95)
    sub = store.get_subscription(sub_id)
    posts = [Post(id=i, text=f"msg{i}", url=f"u{i}") for i in range(96, 106)]
    scores = {f"msg{i}": {CHINA: 0.9 if i in (103, 104) else 0.1}
              for i in range(96, 106)}
    jev = FakeJev(scores)
    sender = FakeSender()
    fetcher = FakeFetcher({"chan": posts},
                          head=ChannelInfo(channel="chan", title="t",
                                           head_id=105, posts=[]))
    pipeline = Pipeline(store, fetcher, jev, sender, default_lang="zh")

    result = await pipeline.preview(sub, pool=5, limit=2)

    assert result.fetched == 5 and result.matched == 2
    assert [post.id for _, post, _ in result.sample] == [104, 103]  # newest first
    assert result.sample[0][0] == "chan"
    assert result.sent is True
    assert all(chat == 42 and len(messages) == 1 for chat, messages in sender.sent)
    chunks = [messages[0] for _, messages in sender.sent]
    assert len(chunks) == 2 and all(chunk.startswith("🧪 试跑样张") for chunk in chunks)
    assert "msg103" in chunks[0] and "msg104" in chunks[1]
    assert "msg104" not in chunks[0] and "msg103" not in chunks[1]
    assert all("msg100" not in chunk for chunk in chunks)
    assert _cursor(store, sub_id) == 95              # the cursor does not move
    assert store.usage_by_kind(user_id=7) == {"fetch": 1, "jev": 5,
                                              "consumed": 5, "deliver": 2}


async def test_preview_delivery_failure_reported(tmp_path):
    """When sending the dry-run sample fails (e.g. the bot was removed from the channel), that is reported in the result."""
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42)
    posts = [Post(id=i, text=f"msg{i}", url=f"u{i}") for i in range(100, 106)]
    sender = FakeSender(error="forbidden")
    fetcher = FakeFetcher({"chan": posts},
                          head=ChannelInfo(channel="chan", title="t",
                                           head_id=105, posts=[]))
    pipeline = Pipeline(store, fetcher, FakeJev({}, default=0.9), sender,
                        default_lang="zh")

    result = await pipeline.preview(store.get_subscription(sub_id), pool=5, limit=2)

    assert result.sent is False
    assert "投递失败" in result.error


def _same_story() -> str:
    return ("今天去医院复查，医生说各项指标恢复正常，"
            "我想把这个好消息分享给一直关心我的朋友们。") * 3


async def test_near_duplicate_from_other_source_skips_only_the_delivery(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    first = _sub(store, 7, 42, source="first")
    second = _sub(store, 7, 42, source="second")
    store.sync_watches()
    body = _same_story()
    fetcher = FakeFetcher({
        "first": [Post(id=101, channel="first", text=body + "\n📮投稿 ☘️频道 🐧聊天",
                       url="https://t.me/first/101")],
        "second": [Post(id=101, channel="second", text=body + "\n关注频道 https://t.me/second",
                        url="https://t.me/second/101")],
    })
    jev, sender = FakeJev(default=0.9), FakeSender()
    pipeline = Pipeline(store, fetcher, jev, sender)
    a = await pipeline.run_watch(store.get_watch("first"))
    b = await pipeline.run_watch(store.get_watch("second"))

    assert (a.matched, a.sent, a.dedup_skipped) == (1, True, 0)
    assert (b.matched, b.sent, b.dedup_skipped) == (1, False, 1)
    assert len(jev.calls) == 2 and len(sender.sent) == 1
    assert _cursor(store, first) == _cursor(store, second) == 101
    assert len(store.recent_deliveries(42)) == 1
    assert store.usage_by_kind(user_id=7)["deliver"] == 1
    assert store._query("SELECT COUNT(*) AS n FROM logs WHERE kind='duplicate_skipped'")[0]["n"] == 1


async def test_dedupe_is_per_destination_across_subscriptions(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42, source="first")
    second = _sub(store, 8, 42, source="second")
    store.add_sub_dest(second, "channel", 43, "other")
    store.sync_watches()
    body = _same_story()
    fetcher = FakeFetcher({
        "first": [Post(id=101, text=body, url="https://t.me/first/101")],
        "second": [Post(id=101, text=body + "\n🛎️转发", url="https://t.me/second/101")],
    })
    sender = FakeSender()
    pipeline = Pipeline(store, fetcher, FakeJev(default=0.9), sender)
    await pipeline.run_watch(store.get_watch("first"))
    res = await pipeline.run_watch(store.get_watch("second"))

    assert res.matched == 1 and res.dedup_skipped == 1 and res.sent
    assert [chat for chat, _ in sender.sent] == [42, 43]
    assert len(store.recent_deliveries(42)) == len(store.recent_deliveries(43)) == 1


async def test_parallel_sources_cannot_both_send_to_the_same_destination(tmp_path):
    class SlowSender(FakeSender):
        async def send(self, chat_id: int, chunks: list[str]) -> None:
            await asyncio.sleep(0.02)  # allow the other watch to race the check
            await super().send(chat_id, chunks)

    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42, source="first")
    _sub(store, 7, 42, source="second")
    store.sync_watches()
    body = _same_story()
    fetcher = FakeFetcher({
        "first": [Post(id=101, text=body, url="https://t.me/first/101")],
        "second": [Post(id=101, text=body, url="https://t.me/second/101")],
    })
    sender = SlowSender()
    pipeline = Pipeline(store, fetcher, FakeJev(default=0.9), sender)
    results = await asyncio.gather(
        pipeline.run_watch(store.get_watch("first")),
        pipeline.run_watch(store.get_watch("second")))
    assert len(sender.sent) == 1
    assert sum(res.dedup_skipped for res in results) == 1
    assert len(store.recent_deliveries(42)) == 1


async def test_failed_send_never_claims_a_delivery(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42)
    store.sync_watches()
    body = _same_story()
    sender = FakeSender(error="forbidden")
    pipeline = Pipeline(store, FakeFetcher({"chan": [Post(id=101, text=body, url="u1")]}),
                        FakeJev(default=0.9), sender)
    res = await pipeline.run_watch(store.get_watch("chan"))
    assert res.error and not res.sent and store.recent_deliveries(42) == []


async def test_partial_batch_failure_retains_earlier_success_in_history(tmp_path):
    class FailSecond(FakeSender):
        async def send(self, chat_id: int, chunks: list[str]) -> None:
            if "另一个独立故事" in chunks[0]:
                raise DeliveryError("second send failed")
            await super().send(chat_id, chunks)

    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42)
    store.sync_watches()
    posts = [
        Post(id=101, text=_same_story(), url="https://t.me/chan/101"),
        Post(id=102, text="另一个独立故事，内容与第一条完全不同，需要单独核验进展。" * 3,
             url="https://t.me/chan/102"),
    ]
    sender = FailSecond()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), FakeJev(default=0.9), sender)
    res = await pipeline.run_watch(store.get_watch("chan"))
    assert res.matched == 2 and res.sent and "second send failed" in res.error
    assert len(sender.sent) == 1
    assert [r["source_url"] for r in store.recent_deliveries(42)] == [posts[0].url]
    assert store.usage_by_kind(user_id=7)["deliver"] == 1


async def test_dry_run_is_not_part_of_real_delivery_history(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    sid = _sub(store, 7, 42, cursor=95)
    body = _same_story()
    store.record_delivery(42, body, "https://t.me/old/101")
    sender = FakeSender()
    fetcher = FakeFetcher({"chan": [Post(id=101, text=body, url="https://t.me/chan/101")]},
                          head=ChannelInfo(channel="chan", title="t", head_id=101, posts=[]))
    pipeline = Pipeline(store, fetcher, FakeJev(default=0.9), sender)
    res = await pipeline.preview(store.get_subscription(sid), pool=10, limit=1)
    assert res.sent and res.dedup_skipped == 0 and len(sender.sent) == 1
    assert len(store.recent_deliveries(42)) == 1  # test sample did not enter history


async def test_same_clipped_opening_does_not_hide_distinct_long_posts(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42, source="first")
    _sub(store, 7, 42, source="second")
    store.sync_watches()
    first_500 = "共同前言" * 125
    assert len(first_500) == 500
    posts = {
        "first": [Post(id=101, text=first_500,
                       dedupe_text=first_500 + "警方已确认调查结果并公布完整报告。" * 15,
                       url="https://t.me/first/101")],
        "second": [Post(id=101, text=first_500,
                        dedupe_text=first_500 + "新研究发现相关数据还需要重新核实。" * 15,
                        url="https://t.me/second/101")],
    }
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher(posts), FakeJev(default=0.9), sender)
    await pipeline.run_watch(store.get_watch("first"))
    res = await pipeline.run_watch(store.get_watch("second"))
    assert res.dedup_skipped == 0 and len(sender.sent) == 2


async def test_unseen_tail_beyond_dedupe_cap_is_not_suppressed(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42, source="first")
    _sub(store, 7, 42, source="second")
    store.sync_watches()
    prefix = "完全相同的正文" * 700
    assert len(prefix) > 4000
    fetcher = FakeFetcher({
        "first": [Post(id=101, text=prefix[:500], dedupe_text=prefix[:3999] + "…",
                       dedupe_truncated=True, url="https://t.me/first/101")],
        "second": [Post(id=101, text=prefix[:500], dedupe_text=prefix[:3999] + "…",
                        dedupe_truncated=True, url="https://t.me/second/101")],
    })
    sender = FakeSender()
    pipeline = Pipeline(store, fetcher, FakeJev(default=0.9), sender)
    await pipeline.run_watch(store.get_watch("first"))
    res = await pipeline.run_watch(store.get_watch("second"))
    assert res.dedup_skipped == 0 and len(sender.sent) == 2
    assert store.recent_deliveries(42) == []


async def test_same_batch_sends_first_repost_and_distinct_third_post(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42)
    store.sync_watches()
    body = _same_story()
    distinct = "另一篇报道来自不同采访，介绍交通规划的路线、施工安排和居民意见。" * 3
    posts = [
        Post(id=101, text=body, url="https://t.me/chan/101"),
        Post(id=102, text="【投稿】" + body + "\n欢迎关注频道 https://t.me/other",
             url="https://t.me/chan/102"),
        Post(id=103, text=distinct, url="https://t.me/chan/103"),
    ]
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), FakeJev(default=0.9), sender)
    res = await pipeline.run_watch(store.get_watch("chan"))
    assert res.matched == 3 and res.dedup_skipped == 1 and res.sent
    assert len(sender.sent) == 2
    assert [r["source_url"] for r in store.recent_deliveries(42)] == [posts[2].url, posts[0].url]
    assert store.usage_by_kind(user_id=7)["deliver"] == 2
    assert _cursor(store, 1) == 103


@pytest.mark.parametrize("age_hours,should_send", [(23, False), (25, True)])
async def test_live_delivery_uses_send_time_not_source_post_time(tmp_path, age_hours, should_send):
    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42)
    store.sync_watches()
    body = _same_story()
    from tgfilter.dedupe import canonicalize
    store.record_delivery(42, canonicalize(body), "https://t.me/other/1",
                          when=datetime.now(timezone.utc) - timedelta(hours=age_hours))
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": [Post(id=101, text=body,
                        url="https://t.me/chan/101",
                        date=datetime.now(timezone.utc) - timedelta(days=10))]}),
                        FakeJev(default=0.9), sender)
    res = await pipeline.run_watch(store.get_watch("chan"))
    assert res.matched == 1
    assert res.sent is should_send
    assert res.dedup_skipped == (0 if should_send else 1)
    assert len(sender.sent) == int(should_send)


async def test_history_survives_pipeline_restart_and_dedupes_another_channel(tmp_path):
    path = str(tmp_path / "d.db")
    store = Store(path)
    _sub(store, 7, 42, source="first")
    _sub(store, 7, 42, source="second")
    store.sync_watches()
    body = _same_story()
    sender = FakeSender()
    await Pipeline(store, FakeFetcher({"first": [Post(id=101, text=body, url="u1")]}),
                   FakeJev(default=0.9), sender).run_watch(store.get_watch("first"))
    store.close()

    reopened = Store(path)
    res = await Pipeline(reopened, FakeFetcher({"second": [Post(id=101, text=body,
                           url="u2")]}), FakeJev(default=0.9), sender
                         ).run_watch(reopened.get_watch("second"))
    assert not res.sent and res.dedup_skipped == 1
    assert len(sender.sent) == 1 and len(reopened.recent_deliveries(42)) == 1
    reopened.close()


async def test_unmatched_post_does_not_poison_delivery_history(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    _sub(store, 7, 42, source="first")
    _sub(store, 7, 42, source="second")
    store.sync_watches()
    body = _same_story()
    fetcher = FakeFetcher({"first": [Post(id=101, text=body, url="u1")],
                           "second": [Post(id=101, text=body, url="u2")]})
    jev, sender = FakeJev(default=0.1), FakeSender()
    pipeline = Pipeline(store, fetcher, jev, sender)
    first = await pipeline.run_watch(store.get_watch("first"))
    assert first.matched == 0 and not sender.sent and store.recent_deliveries(42) == []
    jev.scores[body] = {CHINA: 0.95}
    second = await pipeline.run_watch(store.get_watch("second"))
    assert second.matched == 1 and second.sent and second.dedup_skipped == 0
    assert len(sender.sent) == 1
