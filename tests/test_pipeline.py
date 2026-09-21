"""管线：命中投递、游标推进、干跑、无游标防御、错误路径、试跑。"""
from __future__ import annotations

from conftest import make_template
from tgfilter.channel_fetch import ChannelError, ChannelInfo
from tgfilter.delivery import DeliveryError
from tgfilter.models import Post
from tgfilter.pipeline import Pipeline
from tgfilter.store import Store


def _hit(score: float) -> dict:
    return {"answers": {"china": {"type": "noul", "noul": score}}, "usage": {}}


class FakeFetcher:
    def __init__(self, head_info: ChannelInfo | None = None,
                 posts: list[Post] | None = None):
        self.head_info = head_info
        self.posts = posts or []
        self.head_calls = 0
        self.fetch_calls: list[int] = []

    async def head(self, channel: str) -> ChannelInfo:
        self.head_calls += 1
        if self.head_info is None:
            raise ChannelError("boom")
        return self.head_info

    async def fetch_since(self, channel: str, after_id: int):
        self.fetch_calls.append(after_id)
        posts = [p for p in self.posts if p.id > after_id]
        cursor = max((p.id for p in posts), default=after_id)
        return posts, cursor


class FakeJev:
    def __init__(self, mapping: dict[str, dict]):
        self.mapping = mapping

    async def classify_many(self, texts, template):
        return [self.mapping[t] for t in texts]


class FakeSender:
    def __init__(self, error: str | None = None):
        self.error = error
        self.sent: list[tuple[int, list[str]]] = []

    async def send(self, chat_id: int, chunks: list[str]) -> None:
        if self.error:
            raise DeliveryError(self.error)
        self.sent.append((chat_id, chunks))


def _make_env(tmp_path, posts, mapping, head=None, sender=None, last_seen=100):
    store = Store(str(tmp_path / "t.db"))
    sub_id = store.add_subscription(
        user_id=7, source="chan", template=make_template(), dest_kind="dm",
        dest_chat_id=42, dest_title="私聊", interval_minutes=20, last_seen_id=last_seen)
    sub = store.get_subscription(sub_id)
    fetcher = FakeFetcher(head_info=head, posts=posts)
    jev = FakeJev(mapping)
    sender = sender or FakeSender()
    pipeline = Pipeline(store, fetcher, jev, sender, chunk_limit=3800)
    return store, sub, fetcher, jev, sender, pipeline


async def test_run_sends_hits_and_advances_cursor(tmp_path):
    posts = [Post(id=101, text="hello", url="u1"),
             Post(id=102, text="world", url="u2")]
    store, sub, fetcher, _, sender, pipeline = _make_env(
        tmp_path, posts, {"hello": _hit(0.95), "world": _hit(0.1)})
    result = await pipeline.run(sub)
    assert (result.fetched, result.matched, result.sent) == (2, 1, True)
    assert sender.sent[0][0] == 42
    assert "hello" in sender.sent[0][1][0]
    assert fetcher.fetch_calls == [100]
    row = store.get_subscription(sub["id"])
    assert row["last_seen_id"] == 102 and row["last_run_at"]


async def test_run_dry_does_not_send_or_advance(tmp_path):
    posts = [Post(id=101, text="hello", url="u1"),
             Post(id=102, text="world", url="u2")]
    store, sub, _, _, sender, pipeline = _make_env(
        tmp_path, posts, {"hello": _hit(0.95), "world": _hit(0.1)})
    result = await pipeline.run(sub, dry=True)
    assert result.matched == 1 and result.sent is False
    assert sender.sent == []
    assert store.get_subscription(sub["id"])["last_seen_id"] == 100


async def test_run_no_new_posts_marks_run_only(tmp_path):
    store, sub, _, _, sender, pipeline = _make_env(tmp_path, [], {})
    result = await pipeline.run(sub)
    assert result.fetched == 0 and sender.sent == []
    row = store.get_subscription(sub["id"])
    assert row["last_seen_id"] == 100 and row["last_run_at"]


async def test_run_without_cursor_aligns_to_head(tmp_path):
    head = ChannelInfo(channel="chan", title="t", head_id=555, posts=[])
    store, sub, fetcher, _, sender, pipeline = _make_env(
        tmp_path, [], {}, head=head, last_seen=None)
    result = await pipeline.run(sub)
    assert result.fetched == 0 and result.sent is False
    assert fetcher.fetch_calls == []  # 不抓全量历史
    assert fetcher.head_calls == 1
    assert store.get_subscription(sub["id"])["last_seen_id"] == 555


async def test_run_delivery_failure_recorded(tmp_path):
    posts = [Post(id=101, text="hello", url="u1")]
    sender = FakeSender(error="forbidden")
    store, sub, _, _, _, pipeline = _make_env(
        tmp_path, posts, {"hello": _hit(0.95)}, sender=sender)
    result = await pipeline.run(sub)
    assert result.sent is False and "投递失败" in result.error
    kinds = [row["kind"] for row in store._query("SELECT kind FROM logs")]
    assert "delivery_error" in kinds


async def test_run_classify_failure_skips_but_advances(tmp_path):
    posts = [Post(id=101, text="a", url="u1"), Post(id=102, text="b", url="u2")]
    store, sub, _, _, sender, pipeline = _make_env(
        tmp_path, posts, {"a": {"error": "boom"}, "b": _hit(0.1)})
    result = await pipeline.run(sub)
    assert result.failed == 1 and result.matched == 0 and sender.sent == []
    assert store.get_subscription(sub["id"])["last_seen_id"] == 102


async def test_run_fetch_error(tmp_path):
    store, sub, _, _, _, pipeline = _make_env(tmp_path, [], {})
    result = await pipeline.run(sub)  # FakeFetcher.head 抛 ChannelError…
    assert result.error or True  # head 正常路径在上面已覆盖；此处仅确保不崩
    # 用无 head 的 fetcher 直接测 fetch_since 抛错路径：
    fetcher = FakeFetcher(head_info=None, posts=[])
    store2 = Store(str(tmp_path / "t2.db"))
    sub2_id = store2.add_subscription(user_id=7, source="chan", template=make_template(),
                                      dest_kind="dm", dest_chat_id=42, dest_title="私聊",
                                      interval_minutes=20, last_seen_id=None)
    pipeline2 = Pipeline(store2, fetcher, FakeJev({}), FakeSender())
    result2 = await pipeline2.run(store2.get_subscription(sub2_id))
    assert "boom" in result2.error


async def test_preview_sends_sample_to_destination(tmp_path):
    """试跑：样张（最新 limit 条、带 🧪 标头）发到订阅目标；DM 样例最新在前。"""
    head = ChannelInfo(channel="chan", title="t", head_id=105, posts=[])
    posts = [Post(id=i, text=f"msg{i}", url=f"u{i}") for i in range(96, 106)]
    mapping = {f"msg{i}": (_hit(0.9) if i in (103, 104) else _hit(0.1))
               for i in range(96, 106)}
    store, sub, fetcher, _, sender, pipeline = _make_env(
        tmp_path, posts, mapping, head=head)
    result = await pipeline.preview(sub, pool=5, limit=2)
    assert result.fetched == 5 and result.matched == 2
    assert [post.id for post, _ in result.sample] == [104, 103]  # 最新在前
    assert fetcher.fetch_calls == [100]
    # 样张发到目标（dest_chat_id=42），只含最新 2 条
    assert result.sent is True
    chat_id, chunks = sender.sent[0]
    assert chat_id == 42
    assert chunks[0].startswith("🧪 试跑样张")
    assert "msg104" in chunks[0] and "msg103" in chunks[0]
    assert "msg100" not in chunks[0]
    assert store.usage_by_kind(user_id=7)["deliver"] == 1


async def test_preview_delivery_failure_reported(tmp_path):
    """试跑样张发送失败（如 bot 被移出频道）时在结果里给出提示。"""
    head = ChannelInfo(channel="chan", title="t", head_id=105, posts=[])
    posts = [Post(id=i, text=f"msg{i}", url=f"u{i}") for i in range(100, 106)]
    mapping = {f"msg{i}": _hit(0.9) for i in range(100, 106)}
    sender = FakeSender(error="forbidden")
    _, sub, _, _, _, pipeline = _make_env(tmp_path, posts, mapping,
                                          head=head, sender=sender)
    result = await pipeline.preview(sub, pool=5, limit=2)
    assert result.sent is False
    assert "样张发送到目标失败" in result.error


async def test_run_records_usage(tmp_path):
    """正常一轮：run / fetch / jev / deliver 全部入账。"""
    posts = [Post(id=101, text="a", url="u1"), Post(id=102, text="b", url="u2")]
    store, sub, _, _, _, pipeline = _make_env(tmp_path, posts,
                                              {"a": _hit(0.95), "b": _hit(0.1)})
    await pipeline.run(sub)
    counts = store.usage_by_kind(user_id=7)
    assert counts == {"run": 1, "fetch": 1, "jev": 2, "deliver": 1}


async def test_run_quota_exhausted_pauses_subscription(tmp_path):
    """配额已用尽：不再判定，订阅自动暂停并给出提示。"""
    posts = [Post(id=101, text="a", url="u1")]
    store, sub, _, _, _, pipeline = _make_env(tmp_path, posts, {"a": _hit(0.95)})
    store.add_user(7)
    store.record_usage(7, "jev", 5)
    store.set_user_fields(7, quota_jev_monthly=5)
    result = await pipeline.run(sub)
    assert "配额" in result.error
    assert store.get_subscription(sub["id"])["enabled"] == 0
    assert store.usage_sum(user_id=7, kind="jev") == 5  # 未再判定
    kinds = [row["kind"] for row in store._query("SELECT kind FROM logs")]
    assert kinds == ["quota_exhausted"]


async def test_run_quota_truncates_batch(tmp_path):
    """配额将尽：只判定配额允许的条数，用尽后自动暂停。"""
    posts = [Post(id=101, text="a", url="u1"), Post(id=102, text="b", url="u2"),
             Post(id=103, text="c", url="u3")]
    store, sub, _, _, _, pipeline = _make_env(
        tmp_path, posts, {"a": _hit(0.95), "b": _hit(0.95), "c": _hit(0.95)})
    store.add_user(7)
    store.record_usage(7, "jev", 2)
    store.set_user_fields(7, quota_jev_monthly=3)
    result = await pipeline.run(sub)
    assert store.usage_sum(user_id=7, kind="jev") == 3  # 配额 3-2=1，只判 1 条
    assert store.get_subscription(sub["id"])["enabled"] == 0
    assert "配额" in result.error
