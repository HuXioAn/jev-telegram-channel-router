"""频道轮次管线：联合判定（一次调用服务多模板）、路由、游标、缓存幂等、配额、试跑。"""
from __future__ import annotations

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
    """并集判定打桩：按 (text, instructions) 给分；记录每次调用携带的问题数。"""

    def __init__(self, scores: dict[str, dict[str, float]] | None = None,
                 default: float = 0.1, errors: set[str] | None = None):
        self.scores = scores or {}
        self.default = default
        self.errors = errors or set()
        self.calls: list[tuple[str, int]] = []   # 联合判定调用 (text, 问题数)
        self.local_calls: list[str] = []         # classify_many（试跑）调用
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
         source: str = "chan", template=None, interval: int = 20) -> int:
    return store.add_subscription(
        user_id=user_id, template=template or make_template(),
        interval_minutes=interval,
        sources=[{"source": source, "last_seen_id": cursor}],
        dests=[{"kind": "dm", "chat_id": chat_id, "title": "私聊"}])


def _cursor(store: Store, sub_id: int) -> int | None:
    return store.get_subscription(sub_id)["sources"][0]["last_seen_id"]


# ---------------------------------------------------------------- 基本轮次
async def test_run_watch_sends_hits_and_advances(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42)
    store.sync_watches(20)
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
    assert len(jev.calls) == 2                       # 每条消息一次联合判定
    assert _cursor(store, sub_id) == 102
    watch = store.get_watch("chan")
    assert watch["last_seen_id"] == 102 and watch["last_fetch_at"]
    # 记账：频道级 fetch/jev（真实 token 归频道共享），用户级 consumed/deliver
    assert store.usage_by_kind(user_id=0) == {"fetch": 1, "jev": 2}
    assert store.usage_rollup(user_id=0)["jev"] == {"count": 2, "in": 80, "out": 10}
    assert store.usage_by_kind(user_id=7) == {"consumed": 2, "deliver": 1}


async def test_run_watch_two_templates_one_call_per_post(tmp_path):
    """同频道两个不同模板（不同用户）：每条消息只调用一次 Jev，结果按模板各自路由。"""
    store = Store(str(tmp_path / "t.db"))
    a = _sub(store, 7, 42, template=make_tpl(name="中国"))
    b = _sub(store, 8, 43, template=make_tpl(name="加密", qid="crypto",
                                             instructions=CRYPTO))
    store.sync_watches(20)
    posts = [Post(id=101, text="hello", url="u1"), Post(id=102, text="world", url="u2")]
    jev = FakeJev({"hello": {CHINA: 0.95, CRYPTO: 0.1},
                   "world": {CHINA: 0.1, CRYPTO: 0.9}})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert len(jev.calls) == 2                       # 2 条消息 × 2 模板 → 仍只 2 次调用
    assert all(n == 2 for _, n in jev.calls)         # 每次带上两个模板的问题并集
    assert (result.fetched, result.matched) == (2, 2)
    assert [(chat, "hello" in chunks[0], "world" in chunks[0])
            for chat, chunks in sender.sent] == [(42, True, False), (43, False, True)]
    assert _cursor(store, a) == 102 and _cursor(store, b) == 102
    assert store.usage_by_kind(user_id=7) == {"consumed": 2, "deliver": 1}
    assert store.usage_by_kind(user_id=8) == {"consumed": 2, "deliver": 1}


async def test_run_watch_question_dedup_across_templates(tmp_path):
    """问题级去重：两个模板问题 payload 相同（仅阈值不同）→ 并集只问一次。"""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42, template=make_tpl(name="严", threshold=0.7))
    _sub(store, 8, 43, template=make_tpl(name="宽", threshold=0.5))
    store.sync_watches(20)
    posts = [Post(id=101, text="hello", url="u1")]
    jev = FakeJev({"hello": {CHINA: 0.6}})
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, FakeSender())

    await pipeline.run_watch(store.get_watch("chan"))

    assert jev.calls == [("hello", 1)]               # 并集只有 1 个问题


async def test_run_watch_template_edit_affects_new_posts(tmp_path):
    """编辑模板（问题集变化 → 新指纹）：新消息按新模板判定与路由。"""
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42)
    store.sync_watches(20)
    posts = [Post(id=101, text="a", url="u1")]
    jev = FakeJev({"a": {CHINA: 0.9}})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)
    await pipeline.run_watch(store.get_watch("chan"))
    assert sender.sent and "a" in sender.sent[0][1][0]

    store.set_subscription(sub_id, template_json=make_tpl(name="加密", qid="crypto",
                                                          instructions=CRYPTO
                                                          ).model_dump_json())
    store.mark_watch_fetched("chan", 100)            # 模拟回退再抓
    posts.append(Post(id=102, text="b", url="u2"))
    jev.scores["b"] = {CRYPTO: 0.9}
    await pipeline.run_watch(store.get_watch("chan"))
    # 只判定了新消息 b（a 已有缓存且新指纹在旧消息上不适用——游标已过、不影响）
    assert ("b", 1) in jev.calls
    assert sender.sent[-1][0] == 42 and "b" in sender.sent[-1][1][0]


# ---------------------------------------------------------------- 时间解耦
async def test_run_watch_laggard_sub_gets_backlog(tmp_path):
    """时间解耦：同频道不同订阅游标不同 → 各自补收自己缺的消息，互不影响。"""
    store = Store(str(tmp_path / "b.db"))
    a = _sub(store, 7, 42, cursor=100)
    b = _sub(store, 8, 43, cursor=200)
    store.sync_watches(20)
    assert store.get_watch("chan")["last_seen_id"] == 100   # 物化取订阅游标最小值
    posts = [Post(id=i, text=f"m{i}", url=f"u{i}") for i in range(195, 206)]
    jev = FakeJev({f"m{i}": {CHINA: 0.9} for i in range(195, 206)})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)

    await pipeline.run_watch(store.get_watch("chan"))

    assert len(jev.calls) == 11                      # 每条消息一次
    assert _cursor(store, a) == 205 and _cursor(store, b) == 205
    assert sender.sent[0][0] == 42
    assert "m195" in sender.sent[0][1][0] and "m205" in sender.sent[0][1][0]
    assert sender.sent[1][0] == 43
    assert "m201" in sender.sent[1][1][0] and "m195" not in sender.sent[1][1][0]
    assert store.usage_by_kind(user_id=7)["consumed"] == 11
    assert store.usage_by_kind(user_id=8)["consumed"] == 5


async def test_run_watch_rerun_is_free_and_idempotent(tmp_path):
    """判定缓存 + 消费游标：抓取游标回退重跑不重复调用 Jev、不重复投递。"""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42)
    store.sync_watches(20)
    posts = [Post(id=101, text="hello", url="u1")]
    jev = FakeJev({"hello": {CHINA: 0.9}})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)
    await pipeline.run_watch(store.get_watch("chan"))

    store.mark_watch_fetched("chan", 100)            # 回退抓取游标
    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.cached == 1 and result.judged == 0
    assert len(jev.calls) == 1                       # 未重复调用
    assert len(sender.sent) == 1                     # 未重复投递
    assert store.usage_by_kind(user_id=7) == {"consumed": 1, "deliver": 1}


async def test_run_watch_no_new_posts(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42)
    store.sync_watches(20)
    jev = FakeJev({})
    pipeline = Pipeline(store, FakeFetcher({"chan": []}), jev, FakeSender())

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.fetched == 0 and jev.calls == []
    watch = store.get_watch("chan")
    assert watch["last_fetch_at"] and watch["last_seen_id"] == 100


async def test_run_watch_seeds_cursor_from_head(tmp_path):
    """新建频道首次物化无游标：只播种头部，不抓历史。"""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42, cursor=None)
    store.sync_watches(20)
    assert store.get_watch("chan")["last_seen_id"] is None
    head = ChannelInfo(channel="chan", title="t", head_id=555, posts=[])
    fetcher = FakeFetcher(head=head)
    jev = FakeJev({})
    pipeline = Pipeline(store, fetcher, jev, FakeSender())

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.error == "" and fetcher.fetch_calls == []
    assert fetcher.head_calls == 1
    assert store.get_watch("chan")["last_seen_id"] == 555


# ---------------------------------------------------------------- 错误与配额
async def test_run_watch_fetch_error_marks_fetched(tmp_path):
    """抓取失败：记录错误、更新 last_fetch_at（避免每分钟重试）、游标不动。"""
    store = Store(str(tmp_path / "t.db"))
    _sub(store, 7, 42)
    store.sync_watches(20)
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
    store.sync_watches(20)
    jev = FakeJev({"b": {CHINA: 0.1}}, errors={"a"})
    sender = FakeSender()
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, sender)

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.failed == 1 and result.matched == 0 and sender.sent == []
    assert len(jev.calls) == 2
    assert _cursor(store, sub_id) == 102             # 失败消息跳过、游标照常推进
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
        user_id=7, template=make_template(), interval_minutes=20,
        sources=[{"source": "chan", "last_seen_id": 100}],
        dests=[{"kind": "dm", "chat_id": 42, "title": "私聊"},
               {"kind": "channel", "chat_id": -1005, "title": "测试频道"}])
    store.sync_watches(20)
    posts = [Post(id=101, text="hello", url="u1")]
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}),
                        FakeJev({"hello": {CHINA: 0.9}}), FlakySender())

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert result.sent is True
    assert "投递失败（测试频道）" in result.error
    logs = [row["kind"] for row in store._query("SELECT kind FROM logs")]
    assert "delivery_error" in logs and "delivered" in logs


async def test_run_watch_quota_exhausted_pauses_after_batch(tmp_path):
    """配额用尽：当批完成路由后自动暂停该用户的全部订阅。"""
    store = Store(str(tmp_path / "t.db"))
    store.add_user(7)
    sub_id = _sub(store, 7, 42)
    store.sync_watches(20)
    store.record_usage(7, "consumed", 2)
    store.set_user_fields(7, quota_jev_monthly=3)
    posts = [Post(id=101, text="a", url="u1"), Post(id=102, text="b", url="u2")]
    jev = FakeJev({"a": {CHINA: 0.9}, "b": {CHINA: 0.9}})
    pipeline = Pipeline(store, FakeFetcher({"chan": posts}), jev, FakeSender())

    result = await pipeline.run_watch(store.get_watch("chan"))

    assert "配额" in result.error
    assert store.get_subscription(sub_id)["enabled"] == 0
    assert store.usage_sum(user_id=7, kind="consumed") == 4   # 2 + 本批 2
    logs = [row["kind"] for row in store._query("SELECT kind FROM logs")]
    assert "quota_exhausted" in logs
    # 之后的轮次：该频道仅剩已暂停的订阅 → 视为无观察者，不判定不消费
    jev.calls.clear()
    store.mark_watch_fetched("chan", 100)
    posts.append(Post(id=103, text="c", url="u3"))
    jev.scores["c"] = {CHINA: 0.9}
    await pipeline.run_watch(store.get_watch("chan"))
    assert jev.calls == []
    assert store.usage_sum(user_id=7, kind="consumed") == 4


# ---------------------------------------------------------------- 试跑
async def test_preview_sends_sample_to_destination(tmp_path):
    """试跑：样张（最新 limit 条、带 🧪 标头）发到订阅目标；不推进游标。"""
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
    pipeline = Pipeline(store, fetcher, jev, sender)

    result = await pipeline.preview(sub, pool=5, limit=2)

    assert result.fetched == 5 and result.matched == 2
    assert [post.id for _, post, _ in result.sample] == [104, 103]  # 最新在前
    assert result.sample[0][0] == "chan"
    assert result.sent is True
    chat_id, chunks = sender.sent[0]
    assert chat_id == 42 and chunks[0].startswith("🧪 试跑样张")
    assert "msg104" in chunks[0] and "msg103" in chunks[0]
    assert "msg100" not in chunks[0]
    assert _cursor(store, sub_id) == 95              # 游标不动
    assert store.usage_by_kind(user_id=7) == {"fetch": 1, "jev": 5,
                                              "consumed": 5, "deliver": 1}


async def test_preview_delivery_failure_reported(tmp_path):
    """试跑样张发送失败（如 bot 被移出频道）时在结果里给出提示。"""
    store = Store(str(tmp_path / "t.db"))
    sub_id = _sub(store, 7, 42)
    posts = [Post(id=i, text=f"msg{i}", url=f"u{i}") for i in range(100, 106)]
    sender = FakeSender(error="forbidden")
    fetcher = FakeFetcher({"chan": posts},
                          head=ChannelInfo(channel="chan", title="t",
                                           head_id=105, posts=[]))
    pipeline = Pipeline(store, fetcher, FakeJev({}, default=0.9), sender)

    result = await pipeline.preview(store.get_subscription(sub_id), pool=5, limit=2)

    assert result.sent is False
    assert "投递失败" in result.error
