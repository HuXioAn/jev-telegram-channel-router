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
    assert [s["source"] for s in sub["sources"]] == ["chan"]
    assert sub["sources"][0]["last_seen_id"] == 100
    assert [(d["kind"], d["chat_id"]) for d in sub["dests"]] == [("dm", 1)]
    assert template_of(sub) == template  # JSON 往返无损
    store.set_subscription(sub_id, interval_minutes=30, enabled=0)
    sub = store.get_subscription(sub_id)
    assert sub["interval_minutes"] == 30 and sub["enabled"] == 0
    store.delete_subscription(sub_id)
    assert store.get_subscription(sub_id) is None
    assert store.sub_sources(sub_id) == [] and store.sub_dests(sub_id) == []  # 级联清理


def test_sub_sources_and_dests_n_to_n(tmp_path):
    """n 源 ↔ m 目的地：增删、去重、独立游标。"""
    store = _store(tmp_path)
    sub_id = store.add_subscription(
        user_id=1, template=make_template(), interval_minutes=20,
        sources=[{"source": "a", "last_seen_id": 10},
                 {"source": "b", "last_seen_id": 20}],
        dests=[{"kind": "dm", "chat_id": 1, "title": "私聊"},
               {"kind": "channel", "chat_id": -1005, "title": "频道"}])
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["a", "b"]
    assert [d["chat_id"] for d in sub["dests"]] == [1, -1005]

    store.add_sub_source(sub_id, "a")  # 重复 → 忽略
    store.add_sub_source(sub_id, "c", last_seen_id=30)
    store.add_sub_dest(sub_id, "dm", 1)  # 重复 → 忽略
    store.add_sub_dest(sub_id, "channel", -1006, "频道2")
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["a", "b", "c"]
    assert [d["chat_id"] for d in sub["dests"]] == [1, -1005, -1006]

    store.mark_source_run(sub["sources"][1]["id"], 99)  # 只有 b 的游标推进
    sub = store.get_subscription(sub_id)
    assert [s["last_seen_id"] for s in sub["sources"]] == [10, 99, 30]

    store.remove_sub_source(sub["sources"][0]["id"])
    store.remove_sub_dest(sub["dests"][0]["id"])
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["b", "c"]
    assert [d["chat_id"] for d in sub["dests"]] == [-1005, -1006]


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
    assert sub["sources"][0]["last_seen_id"] == 333 and sub["last_run_at"]


def test_logs_recorded(tmp_path):
    store = _store(tmp_path)
    store.log(3, "delivered", "2 hits")
    rows = store._query("SELECT * FROM logs")
    assert rows[0]["kind"] == "delivered" and rows[0]["sub_id"] == 3


def test_usage_recording_and_aggregates(tmp_path):
    store = _store(tmp_path)
    store.record_usage(1, "jev", 3, sub_id=9, input_tokens=100, output_tokens=10)
    store.record_usage(1, "jev", 2)
    store.record_usage(2, "jev", 5)
    store.record_usage(1, "llm", 1, detail="desc")
    assert store.usage_sum(user_id=1, kind="jev") == 5
    assert store.usage_sum(kind="jev") == 10
    assert store.usage_by_kind(user_id=1) == {"jev": 5, "llm": 1}
    assert store.usage_rollup(user_id=1)["jev"] == {"count": 5, "in": 100, "out": 10}
    rows = store.usage_rows()
    assert {(r["user_id"], r["kind"], r["s"], r["tin"], r["tout"])
            for r in rows} == {
        (1, "jev", 5, 100, 10), (2, "jev", 5, 0, 0), (1, "llm", 1, 0, 0)}
    assert store.recent_usage(user_id=1, limit=1)[0]["detail"] == "desc"


def test_user_fields_and_counts(tmp_path):
    store = _store(tmp_path)
    store.add_user(1, "a", default_status="blocked")
    assert store.get_user(1)["status"] == "blocked"
    store.set_user_fields(1, status="active", max_subs=3,
                          quota_jev_monthly=100, note="vip")
    user = store.get_user(1)
    assert (user["status"], user["max_subs"],
            user["quota_jev_monthly"], user["note"]) == ("active", 3, 100, "vip")
    assert store.count_users() == {"total": 1, "active": 1, "blocked": 0}
    store.add_subscription(user_id=1, source="c", template=make_template(),
                           dest_kind="dm", dest_chat_id=1, dest_title="私聊",
                           interval_minutes=20, last_seen_id=1)
    assert store.count_subscriptions() == {"total": 1, "enabled": 1}
    assert store.count_subscriptions_for(1) == 1


def test_migration_adds_new_user_columns(tmp_path):
    """旧库（无 status/配额列）初始化时自动迁移。"""
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT,"
                 " created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO users(id, username, created_at) VALUES(1, 'old', '2026-01-01')")
    conn.commit()
    conn.close()
    store = Store(path)
    user = store.get_user(1)
    assert user["status"] == "active" and user["max_subs"] is None
    assert user["quota_jev_monthly"] is None


def test_migration_adds_usage_token_columns(tmp_path):
    """旧版 usage 表（无 token 列）初始化时自动迁移，可直接记录与聚合。"""
    import sqlite3

    path = str(tmp_path / "old_usage.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT,"
        " created_at TEXT NOT NULL);"
        "CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
        " user_id INTEGER NOT NULL, sub_id INTEGER, kind TEXT NOT NULL,"
        " qty INTEGER NOT NULL DEFAULT 1, detail TEXT);")
    conn.commit()
    conn.close()
    store = Store(path)
    store.record_usage(1, "jev", 2, input_tokens=50, output_tokens=5)
    assert store.usage_rollup(user_id=1)["jev"] == {"count": 2, "in": 50, "out": 5}


def test_migration_splits_legacy_subscription(tmp_path):
    """旧库（单源单目的地 subscriptions）升级：重建表并拆分到 sub_sources/sub_dests。"""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL, source TEXT NOT NULL, template_json TEXT NOT NULL,"
        " dest_kind TEXT NOT NULL, dest_chat_id INTEGER NOT NULL, dest_title TEXT,"
        " interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,"
        " last_seen_id INTEGER, last_run_at TEXT, created_at TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO subscriptions(user_id, source, template_json, dest_kind,"
        " dest_chat_id, dest_title, interval_minutes, enabled, last_seen_id, created_at)"
        " VALUES(1, 'legacy', ?, 'dm', 1, '私聊', 20, 1, 500, '2026-01-01')",
        (make_template().model_dump_json(),))
    conn.commit()
    conn.close()
    store = Store(path)
    sub = store.get_subscription(1)
    assert [s["source"] for s in sub["sources"]] == ["legacy"]
    assert sub["sources"][0]["last_seen_id"] == 500
    assert [(d["kind"], d["chat_id"]) for d in sub["dests"]] == [("dm", 1)]
    assert template_of(sub) == make_template()
    cols = {row["name"] for row in store._query("PRAGMA table_info(subscriptions)")}
    assert "source" not in cols and "dest_chat_id" not in cols  # 旧列已移除
