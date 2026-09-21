"""SQLite 存储：用户 / 频道 / 订阅 / 到期判定 / 日志。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import make_template
from tgfilter.store import Store, template_of


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def test_user_upsert(tmp_path):
    store = _store(tmp_path)
    store.add_user(1, "a")
    store.add_user(1, "b")  # 重复 → 更新用户名
    rows = store._query("SELECT * FROM users")
    assert len(rows) == 1 and rows[0]["username"] == "b"


def test_chat_lifecycle(tmp_path):
    store = _store(tmp_path)
    store.upsert_chat(-100123, "channel", "我的频道", 7)
    store.upsert_chat(-100123, "channel", "改名后", 7)
    chats = store.list_chats(added_by=7)
    assert len(chats) == 1 and chats[0]["title"] == "改名后"
    assert store.list_chats(added_by=8) == []
    assert store.get_chat(-100123)["title"] == "改名后"
    store.remove_chat(-100123)
    assert store.get_chat(-100123) is None


def test_subscription_crud_and_template_roundtrip(tmp_path):
    store = _store(tmp_path)
    template = make_template()
    sub_id = store.add_subscription(user_id=1, source="chan", template=template,
                                    dest_kind="dm", dest_chat_id=1, dest_title="私聊",
                                    interval_minutes=20, last_seen_id=100)
    sub = store.get_subscription(sub_id)
    assert sub["source"] == "chan"
    assert template_of(sub) == template  # JSON 往返无损
    store.set_subscription(sub_id, interval_minutes=30, enabled=0)
    sub = store.get_subscription(sub_id)
    assert sub["interval_minutes"] == 30 and sub["enabled"] == 0
    store.delete_subscription(sub_id)
    assert store.get_subscription(sub_id) is None


def test_due_subscriptions_respects_interval_and_enabled(tmp_path):
    store = _store(tmp_path)
    sub_id = store.add_subscription(user_id=1, source="chan", template=make_template(),
                                    dest_kind="dm", dest_chat_id=1, dest_title="私聊",
                                    interval_minutes=20, last_seen_id=100)
    now = datetime.now(timezone.utc)
    assert [s["id"] for s in store.due_subscriptions(now)] == [sub_id]  # 从未跑过 → 到期
    store.mark_run(sub_id, 120)
    assert store.due_subscriptions(now) == []  # 刚跑过
    later = now + timedelta(minutes=21)
    assert [s["id"] for s in store.due_subscriptions(later)] == [sub_id]
    store.set_subscription(sub_id, enabled=0)
    assert store.due_subscriptions(later) == []  # 暂停后不再到期


def test_mark_run_updates_cursor_and_time(tmp_path):
    store = _store(tmp_path)
    sub_id = store.add_subscription(user_id=1, source="chan", template=make_template(),
                                    dest_kind="dm", dest_chat_id=1, dest_title="私聊",
                                    interval_minutes=20, last_seen_id=100)
    store.mark_run(sub_id, 333)
    sub = store.get_subscription(sub_id)
    assert sub["last_seen_id"] == 333 and sub["last_run_at"]


def test_logs_recorded(tmp_path):
    store = _store(tmp_path)
    store.log(3, "delivered", "2 hits")
    rows = store._query("SELECT * FROM logs")
    assert rows[0]["kind"] == "delivered" and rows[0]["sub_id"] == 3
