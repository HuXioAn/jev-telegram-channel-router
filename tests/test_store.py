"""SQLite storage: users / chats / subscriptions / due checks / logs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import make_template
from tgfilter.store import Store, template_of


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def test_user_upsert(tmp_path):
    store = _store(tmp_path)
    store.add_user(1, "a")
    store.add_user(1, "b")  # duplicate → updates the username
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
                                    last_seen_id=100)
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["chan"]
    assert sub["sources"][0]["last_seen_id"] == 100
    assert [(d["kind"], d["chat_id"]) for d in sub["dests"]] == [("dm", 1)]
    assert template_of(sub) == template  # lossless JSON round trip
    store.set_subscription(sub_id, enabled=0)
    sub = store.get_subscription(sub_id)
    assert sub["enabled"] == 0 and "interval_minutes" not in sub  # single global schedule
    store.delete_subscription(sub_id)
    assert store.get_subscription(sub_id) is None
    assert store.sub_sources(sub_id) == [] and store.sub_dests(sub_id) == []  # cascade cleanup


def test_sub_sources_and_dests_n_to_n(tmp_path):
    """n sources ↔ m destinations: add/remove, dedup, independent cursors."""
    store = _store(tmp_path)
    sub_id = store.add_subscription(
        user_id=1, template=make_template(),
        sources=[{"source": "a", "last_seen_id": 10},
                 {"source": "b", "last_seen_id": 20}],
        dests=[{"kind": "dm", "chat_id": 1, "title": "私聊"},
               {"kind": "channel", "chat_id": -1005, "title": "频道"}])
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["a", "b"]
    assert [d["chat_id"] for d in sub["dests"]] == [1, -1005]

    store.add_sub_source(sub_id, "a")  # duplicate → ignored
    store.add_sub_source(sub_id, "c", last_seen_id=30)
    store.add_sub_dest(sub_id, "dm", 1)  # duplicate → ignored
    store.add_sub_dest(sub_id, "channel", -1006, "频道2")
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["a", "b", "c"]
    assert [d["chat_id"] for d in sub["dests"]] == [1, -1005, -1006]

    store.mark_source_run(sub["sources"][1]["id"], 99)  # only b's cursor advances
    sub = store.get_subscription(sub_id)
    assert [s["last_seen_id"] for s in sub["sources"]] == [10, 99, 30]

    store.remove_sub_source(sub["sources"][0]["id"])
    store.remove_sub_dest(sub["dests"][0]["id"])
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["b", "c"]
    assert [d["chat_id"] for d in sub["dests"]] == [-1005, -1006]


def test_watches_materialize_due_and_retire(tmp_path):
    """Channel-level scheduling: materialization (minimum cursor), due checks against
    the single global interval, retirement and re-materialization."""
    store = _store(tmp_path)
    store.add_subscription(user_id=1, source="chan", template=make_template(),
                           dest_kind="dm", dest_chat_id=1, dest_title="私聊",
                           last_seen_id=100)
    b_id = store.add_subscription(user_id=2, source="chan", template=make_template(),
                                  dest_kind="dm", dest_chat_id=2, dest_title="私聊",
                                  last_seen_id=150)
    store.sync_watches()
    watch = store.get_watch("chan")
    assert watch["last_seen_id"] == 100            # minimum cursor (no lost messages)

    now = datetime.now(timezone.utc)
    assert [w["channel"] for w in store.due_watches(now, 20)] == ["chan"]  # never fetched → due
    store.mark_watch_fetched("chan", 160, when=now.isoformat())
    assert store.due_watches(now, 20) == []
    later = [w["channel"] for w in store.due_watches(now + timedelta(minutes=20), 20)]
    assert later == ["chan"]
    watch = store.get_watch("chan")
    assert watch["last_seen_id"] == 160 and watch["last_fetch_at"]

    # one global interval for every channel, admin-configurable (settings table)
    assert store.fetch_interval_minutes(20) == 20       # unset → configured default
    store.set_fetch_interval_minutes(5)
    assert store.fetch_interval_minutes(20) == 5
    assert store.due_watches(now + timedelta(minutes=5),
                             store.fetch_interval_minutes(20))[0]["channel"] == "chan"

    store.set_subscription(b_id, enabled=0)        # paused no longer counts as a watcher
    store.sync_watches()

    store.delete_subscription(b_id)
    store.delete_subscription(1)
    store.sync_watches()
    assert store.get_watch("chan") is None         # no enabled watchers → retired
    store.add_subscription(user_id=3, source="chan", template=make_template(),
                           dest_kind="dm", dest_chat_id=3, dest_title="私聊",
                           last_seen_id=200)
    store.sync_watches()
    assert store.get_watch("chan")["last_seen_id"] == 200      # re-materialized


def test_store_reopen_materializes_watches(tmp_path):
    """Old database upgrade: on reopen, materialize the source channels of existing subscriptions into watches."""
    path = str(tmp_path / "test.db")
    store = Store(path)
    store.add_subscription(user_id=1, source="chan", template=make_template(),
                           dest_kind="dm", dest_chat_id=1, dest_title="私聊",
                           last_seen_id=100)
    reopened = Store(path)
    watch = reopened.get_watch("chan")
    assert watch["last_seen_id"] == 100


def test_judgments_cache_roundtrip_and_prune(tmp_path):
    store = _store(tmp_path)
    payload = {"tfp1": {"china": {"type": "noul", "noul": 0.9}}}
    store.save_judgment("chan", 101, payload)
    assert store.judgments_for("chan", [101, 102]) == {101: payload}
    store.save_judgment("chan", 101, {"tfp1": {"china": None}})   # overwrite
    assert store.judgments_for("chan", [101]) == {101: {"tfp1": {"china": None}}}
    assert store.judgments_for("other", [101]) == {}
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store._run("UPDATE judgments SET ts=?", (old,))
    assert store.prune_judgments(days=7) == 1
    assert store.judgments_for("chan", [101]) == {}


def test_watchers_of_only_enabled(tmp_path):
    store = _store(tmp_path)
    a = store.add_subscription(user_id=1, source="chan", template=make_template(),
                               dest_kind="dm", dest_chat_id=1, dest_title="私聊",
                               last_seen_id=100)
    store.add_subscription(user_id=2, source="chan", template=make_template(),
                           dest_kind="dm", dest_chat_id=2, dest_title="私聊",
                           last_seen_id=100)
    store.add_subscription(user_id=3, source="other", template=make_template(),
                           dest_kind="dm", dest_chat_id=3, dest_title="私聊",
                           last_seen_id=100)
    assert [w["user_id"] for w in store.watchers_of("chan")] == [1, 2]
    store.set_subscription(a, enabled=0)
    assert [w["user_id"] for w in store.watchers_of("chan")] == [2]
    assert store.watchers_of("nothing") == []


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
                           last_seen_id=1)
    assert store.count_subscriptions() == {"total": 1, "enabled": 1}
    assert store.count_subscriptions_for(1) == 1


def test_migration_adds_new_user_columns(tmp_path):
    """An old database (without status/quota columns) migrates automatically on init."""
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
    """A legacy usage table (without token columns) migrates automatically on init and can immediately record and roll up."""
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
    """Old database (single-source/single-destination subscriptions) upgrade: rebuild the table and split into sub_sources/sub_dests."""
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
    assert "source" not in cols and "dest_chat_id" not in cols  # legacy columns removed
    assert "interval_minutes" not in cols                      # global interval since


def test_migration_drops_interval_columns(tmp_path):
    """Old database (per-channel / per-subscription intervals): columns dropped on
    open, data preserved."""
    import sqlite3

    path = str(tmp_path / "old_interval.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL, template_json TEXT NOT NULL,"
        " interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,"
        " last_run_at TEXT, created_at TEXT NOT NULL);"
        "CREATE TABLE sub_sources (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " sub_id INTEGER NOT NULL, source TEXT NOT NULL, last_seen_id INTEGER,"
        " UNIQUE(sub_id, source));"
        "CREATE TABLE sub_dests (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " sub_id INTEGER NOT NULL, kind TEXT NOT NULL, chat_id INTEGER NOT NULL,"
        " title TEXT, UNIQUE(sub_id, chat_id));"
        "CREATE TABLE watches (channel TEXT PRIMARY KEY, last_seen_id INTEGER,"
        " interval_minutes INTEGER NOT NULL, last_fetch_at TEXT);")
    conn.execute(
        "INSERT INTO subscriptions(user_id, template_json, interval_minutes,"
        " created_at) VALUES(1, ?, 20, '2026-01-01')",
        (make_template().model_dump_json(),))
    conn.execute("INSERT INTO sub_sources(sub_id, source, last_seen_id)"
                 " VALUES(1, 'chan', 500)")
    conn.execute("INSERT INTO watches(channel, last_seen_id, interval_minutes)"
                 " VALUES('chan', 500, 10)")
    conn.commit()
    conn.close()
    store = Store(path)
    for table in ("subscriptions", "watches"):
        cols = {row["name"] for row in store._query(f"PRAGMA table_info({table})")}
        assert "interval_minutes" not in cols
    sub = store.get_subscription(1)
    assert [s["source"] for s in sub["sources"]] == ["chan"]
    watch = store.get_watch("chan")
    assert watch["last_seen_id"] == 500 and watch["last_fetch_at"] is None
