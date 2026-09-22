"""SQLite storage layer: users / chats / subscriptions / watches / judgments / logs."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import i18n
from .models import Template

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    max_subs INTEGER,
    quota_jev_monthly INTEGER,
    note TEXT,
    lang TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT,
    added_by INTEGER,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    template_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sub_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    last_seen_id INTEGER,
    UNIQUE(sub_id, source)
);
CREATE INDEX IF NOT EXISTS idx_sub_sources ON sub_sources(sub_id);
CREATE TABLE IF NOT EXISTS sub_dests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT,
    UNIQUE(sub_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_sub_dests ON sub_dests(sub_id);
CREATE TABLE IF NOT EXISTS watches (
    channel TEXT PRIMARY KEY,
    last_seen_id INTEGER,
    last_fetch_at TEXT
);
CREATE TABLE IF NOT EXISTS judgments (
    channel TEXT NOT NULL,
    post_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (channel, post_id)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id INTEGER,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    sub_id INTEGER,
    kind TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage(user_id, ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def month_start(now: datetime | None = None) -> datetime:
    """Start of the current month (UTC), used for monthly quota accounting."""
    moment = now or datetime.now(timezone.utc)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def template_of(row: dict) -> Template:
    """Parse template_json from a subscription row into a Template."""
    return Template.model_validate_json(row["template_json"])


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
        # Uniform for old and new databases: materialize the source channels of enabled subscriptions into watches (idempotent, only adds new ones)
        self.sync_watches()

    def _migrate(self) -> None:
        """Smooth upgrade of old databases: backfill columns added later."""
        additions = {
            "users": {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "max_subs": "INTEGER",
                "quota_jev_monthly": "INTEGER",
                "note": "TEXT",
                "lang": "TEXT",
            },
            "usage": {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in additions.items():
            cols = {row["name"] for row in
                    self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # single source, single destination -> n:n: rebuild the old subscriptions, splitting data into sub_sources / sub_dests
        sub_cols = {row["name"] for row in
                    self._conn.execute("PRAGMA table_info(subscriptions)")}
        if "source" in sub_cols:
            self._conn.execute("ALTER TABLE subscriptions RENAME TO subscriptions_old")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO subscriptions(id, user_id, template_json,"
                " enabled, last_run_at, created_at) "
                "SELECT id, user_id, template_json, enabled,"
                " last_run_at, created_at FROM subscriptions_old")
            self._conn.execute(
                "INSERT OR IGNORE INTO sub_sources(sub_id, source, last_seen_id) "
                "SELECT id, source, last_seen_id FROM subscriptions_old")
            self._conn.execute(
                "INSERT OR IGNORE INTO sub_dests(sub_id, kind, chat_id, title) "
                "SELECT id, dest_kind, dest_chat_id, dest_title FROM subscriptions_old")
            self._conn.execute("DROP TABLE subscriptions_old")
        # Per-channel / per-subscription refresh intervals were replaced by one
        # global interval (settings.fetch_interval_minutes): rebuild both tables.
        for table, copy_cols in (
                ("subscriptions",
                 "id, user_id, template_json, enabled, last_run_at, created_at"),
                ("watches", "channel, last_seen_id, last_fetch_at")):
            cols = {row["name"] for row in
                    self._conn.execute(f"PRAGMA table_info({table})")}
            if "interval_minutes" not in cols:
                continue
            self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                f"INSERT INTO {table}({copy_cols}) SELECT {copy_cols} FROM {table}_old")
            self._conn.execute(f"DROP TABLE {table}_old")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- users
    def add_user(self, user_id: int, username: str = "",
                 default_status: str = "active") -> None:
        self._run(
            "INSERT INTO users(id, username, status, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
            (user_id, username, default_status, _now()))

    def get_user(self, user_id: int) -> dict | None:
        rows = self._query("SELECT * FROM users WHERE id=?", (user_id,))
        return rows[0] if rows else None

    def list_users(self) -> list[dict]:
        return self._query("SELECT * FROM users ORDER BY created_at")

    def set_user_fields(self, user_id: int, **fields) -> None:
        allowed = {"status", "max_subs", "quota_jev_monthly", "note", "username"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        clause = ", ".join(f"{key}=?" for key in updates)
        self._run(f"UPDATE users SET {clause} WHERE id=?",
                  (*updates.values(), user_id))

    def count_users(self) -> dict[str, int]:
        result = {"total": 0, "active": 0, "blocked": 0}
        for row in self._query("SELECT status, COUNT(*) AS n FROM users GROUP BY status"):
            result["total"] += row["n"]
            result[row["status"]] = result.get(row["status"], 0) + row["n"]
        return result

    # --------------------------------------------------- settings & language
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._run("INSERT INTO settings(key, value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def fetch_interval_minutes(self, default: int) -> int:
        """The single global channel-refresh interval (minutes).

        Admin-set value from the settings table, else the configured default.
        """
        raw = self.get_setting("fetch_interval_minutes")
        try:
            return max(1, int(raw)) if raw else max(1, int(default))
        except ValueError:
            return max(1, int(default))

    def set_fetch_interval_minutes(self, minutes: int) -> None:
        self.set_setting("fetch_interval_minutes", str(max(1, int(minutes))))

    def set_user_lang(self, user_id: int, lang: str) -> None:
        # Upsert: a user may pick a language before any explicit add_user call.
        self._run(
            "INSERT INTO users(id, lang, created_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET lang=excluded.lang",
            (user_id, lang, _now()))

    def language_for(self, user_id: int, default: str = "en") -> str:
        """Resolve a user's UI language: personal choice → instance default.

        The instance default comes from the settings table (set via /admin lang)
        and falls back to the configured default. Codes are normalized so that
        e.g. "zh-hans" resolves to "zh"; anything unknown resolves to English.
        """
        rows = self._query("SELECT lang FROM users WHERE id=?", (user_id,))
        if rows and rows[0]["lang"]:
            return i18n.resolve(rows[0]["lang"])
        return i18n.resolve(self.get_setting("default_lang") or default)

    # ------------------------------------------------------------- chats
    def upsert_chat(self, chat_id: int, kind: str, title: str, added_by: int | None) -> None:
        self._run(
            "INSERT INTO chats(chat_id, kind, title, added_by, added_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET kind=excluded.kind, title=excluded.title",
            (chat_id, kind, title, added_by, _now()))

    def remove_chat(self, chat_id: int) -> None:
        self._run("DELETE FROM chats WHERE chat_id=?", (chat_id,))

    def list_chats(self, added_by: int | None = None) -> list[dict]:
        if added_by is None:
            return self._query("SELECT * FROM chats ORDER BY added_at")
        return self._query("SELECT * FROM chats WHERE added_by=? ORDER BY added_at", (added_by,))

    def get_chat(self, chat_id: int) -> dict | None:
        rows = self._query("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
        return rows[0] if rows else None

    # ------------------------------------------------------ stats / usage
    def count_subscriptions(self) -> dict[str, int]:
        rows = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(enabled),0) AS e FROM subscriptions")
        row = rows[0] if rows else {"n": 0, "e": 0}
        return {"total": int(row["n"]), "enabled": int(row["e"])}

    def count_subscriptions_for(self, user_id: int) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM subscriptions WHERE user_id=?",
                           (user_id,))
        return int(rows[0]["n"]) if rows else 0

    def record_usage(self, user_id: int, kind: str, qty: int = 1,
                     sub_id: int | None = None, detail: str = "",
                     input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Record one usage entry: kind ∈ jev/llm/run/fetch/deliver; token counts are the real API values."""
        self._run(
            "INSERT INTO usage(ts, user_id, sub_id, kind, qty, input_tokens, output_tokens, detail) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (_now(), user_id, sub_id, kind, max(0, int(qty)),
             max(0, int(input_tokens)), max(0, int(output_tokens)),
             detail[:500] or None))

    def usage_sum(self, user_id: int | None = None, kind: str | None = None,
                  since: datetime | None = None) -> int:
        sql, args = "SELECT COALESCE(SUM(qty),0) AS s FROM usage WHERE 1=1", []
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        if since is not None:
            sql += " AND ts>=?"
            args.append(since.isoformat())
        rows = self._query(sql, tuple(args))
        return int(rows[0]["s"])

    def usage_rollup(self, user_id: int | None = None,
                     since: datetime | None = None) -> dict[str, dict[str, int]]:
        """Roll up by kind: {"jev": {"count": n, "in": tokens, "out": tokens}, ...}."""
        sql, args = ("SELECT kind, COALESCE(SUM(qty),0) AS s, "
                     "COALESCE(SUM(input_tokens),0) AS tin, "
                     "COALESCE(SUM(output_tokens),0) AS tout "
                     "FROM usage WHERE 1=1"), []
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        if since is not None:
            sql += " AND ts>=?"
            args.append(since.isoformat())
        sql += " GROUP BY kind"
        return {row["kind"]: {"count": int(row["s"]), "in": int(row["tin"]),
                              "out": int(row["tout"])}
                for row in self._query(sql, tuple(args))}

    def usage_by_kind(self, user_id: int | None = None,
                      since: datetime | None = None) -> dict[str, int]:
        return {kind: item["count"]
                for kind, item in self.usage_rollup(user_id, since).items()}

    def usage_rows(self, since: datetime | None = None) -> list[dict]:
        """Roll up by (user_id, kind) (tokens included), for the admin view to aggregate."""
        sql, args = ("SELECT user_id, kind, COALESCE(SUM(qty),0) AS s, "
                     "COALESCE(SUM(input_tokens),0) AS tin, "
                     "COALESCE(SUM(output_tokens),0) AS tout "
                     "FROM usage WHERE 1=1"), []
        if since is not None:
            sql += " AND ts>=?"
            args.append(since.isoformat())
        sql += " GROUP BY user_id, kind"
        return self._query(sql, tuple(args))

    def recent_usage(self, user_id: int | None = None, limit: int = 10) -> list[dict]:
        sql, args = "SELECT * FROM usage WHERE 1=1", []
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self._query(sql, tuple(args))

    # ----------------------------------------------------- subscriptions
    def add_subscription(self, *, user_id: int, template: Template,
                         sources: list[dict] | None = None,
                         dests: list[dict] | None = None,
                         source: str | None = None,
                         last_seen_id: int | None = None,
                         dest_kind: str | None = None,
                         dest_chat_id: int | None = None,
                         dest_title: str | None = None) -> int:
        """Create a subscription (n sources -> m destinations). The legacy single-source/single-destination signature is automatically turned into single-element lists."""
        if source is not None:
            sources = [{"source": source, "last_seen_id": last_seen_id}]
        if dest_kind is not None and dest_chat_id is not None:
            dests = [{"kind": dest_kind, "chat_id": dest_chat_id, "title": dest_title}]
        cursor = self._run(
            "INSERT INTO subscriptions(user_id, template_json,"
            " enabled, created_at) VALUES(?,?,1,?)",
            (user_id, template.model_dump_json(), _now()))
        sub_id = int(cursor.lastrowid or 0)
        for item in sources or []:
            self.add_sub_source(sub_id, item["source"], item.get("last_seen_id"))
        for item in dests or []:
            self.add_sub_dest(sub_id, item["kind"], int(item["chat_id"]), item.get("title"))
        return sub_id

    def add_sub_source(self, sub_id: int, source: str,
                       last_seen_id: int | None = None) -> None:
        self._run(
            "INSERT INTO sub_sources(sub_id, source, last_seen_id) VALUES(?,?,?) "
            "ON CONFLICT(sub_id, source) DO NOTHING",
            (sub_id, source, last_seen_id))

    def remove_sub_source(self, source_id: int) -> None:
        self._run("DELETE FROM sub_sources WHERE id=?", (source_id,))

    def sub_sources(self, sub_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM sub_sources WHERE sub_id=? ORDER BY id", (sub_id,))

    def add_sub_dest(self, sub_id: int, kind: str, chat_id: int,
                     title: str | None = None) -> None:
        self._run(
            "INSERT INTO sub_dests(sub_id, kind, chat_id, title) VALUES(?,?,?,?) "
            "ON CONFLICT(sub_id, chat_id) DO UPDATE SET kind=excluded.kind,"
            " title=excluded.title",
            (sub_id, kind, chat_id, title))

    def remove_sub_dest(self, dest_id: int) -> None:
        self._run("DELETE FROM sub_dests WHERE id=?", (dest_id,))

    def sub_dests(self, sub_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM sub_dests WHERE sub_id=? ORDER BY id", (sub_id,))

    def list_subscriptions(self, user_id: int | None = None) -> list[dict]:
        if user_id is None:
            rows = self._query("SELECT * FROM subscriptions ORDER BY id")
        else:
            rows = self._query(
                "SELECT * FROM subscriptions WHERE user_id=? ORDER BY id", (user_id,))
        return [self._with_children(row) for row in rows]

    def get_subscription(self, sub_id: int) -> dict | None:
        rows = self._query("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
        return self._with_children(rows[0]) if rows else None

    def _with_children(self, sub: dict) -> dict:
        sub["sources"] = self.sub_sources(sub["id"])
        sub["dests"] = self.sub_dests(sub["id"])
        return sub

    def mark_source_run(self, source_id: int, last_seen_id: int | None) -> None:
        """Advance the fetch cursor of a single source channel (each source is independent)."""
        self._run("UPDATE sub_sources SET last_seen_id=? WHERE id=?",
                  (last_seen_id, source_id))

    def set_subscription(self, sub_id: int, **fields) -> None:
        allowed = {"template_json", "enabled", "last_run_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        clause = ", ".join(f"{key}=?" for key in updates)
        self._run(f"UPDATE subscriptions SET {clause} WHERE id=?",
                  (*updates.values(), sub_id))

    def delete_subscription(self, sub_id: int) -> None:
        self._run("DELETE FROM sub_sources WHERE sub_id=?", (sub_id,))
        self._run("DELETE FROM sub_dests WHERE sub_id=?", (sub_id,))
        self._run("DELETE FROM subscriptions WHERE id=?", (sub_id,))

    # ----------------------------------------------- watches (channel-level scheduling)
    def sync_watches(self) -> None:
        """Materialize: watches = union of the source channels of enabled subscriptions (idempotent).

        - New channel: the cursor is the minimum cursor over all of that channel's subscriptions (no lost messages).
        - Existing channel: kept as is; retired and deleted when it has no enabled watchers.
        Refresh timing is the single global interval (see fetch_interval_minutes).
        """
        rows = self._query(
            "SELECT s.source AS channel, MIN(s.last_seen_id) AS cursor"
            " FROM sub_sources s JOIN subscriptions sub ON sub.id = s.sub_id"
            " WHERE sub.enabled=1 GROUP BY s.source")
        live = {row["channel"] for row in rows}
        existing = {row["channel"] for row in self._query("SELECT channel FROM watches")}
        for row in rows:
            if row["channel"] in existing:
                continue
            self._run(
                "INSERT INTO watches(channel, last_seen_id, last_fetch_at)"
                " VALUES(?,?,NULL)",
                (row["channel"], row["cursor"]))
        for channel in existing - live:
            self._run("DELETE FROM watches WHERE channel=?", (channel,))

    def due_watches(self, now: datetime, interval_minutes: int) -> list[dict]:
        """Channels that are due (never fetched, or last fetched longer ago than the global interval)."""
        due = []
        for row in self._query("SELECT * FROM watches"):
            last = _parse_ts(row["last_fetch_at"])
            if last is None or now - last >= timedelta(minutes=interval_minutes):
                due.append(row)
        return due

    def list_watches(self) -> list[dict]:
        return self._query("SELECT * FROM watches ORDER BY channel")

    def get_watch(self, channel: str) -> dict | None:
        rows = self._query("SELECT * FROM watches WHERE channel=?", (channel,))
        return rows[0] if rows else None

    def mark_watch_fetched(self, channel: str, last_seen_id: int | None = None,
                           when: str | None = None) -> None:
        """Record the channel fetch time; optionally advance the fetch cursor (called after routing completes)."""
        if last_seen_id is None:
            self._run("UPDATE watches SET last_fetch_at=? WHERE channel=?",
                      (when or _now(), channel))
        else:
            self._run(
                "UPDATE watches SET last_seen_id=?, last_fetch_at=? WHERE channel=?",
                (last_seen_id, when or _now(), channel))

    def watchers_of(self, channel: str) -> list[dict]:
        """The enabled subscriptions watching a given channel (with source/destination)."""
        rows = self._query(
            "SELECT DISTINCT sub.* FROM subscriptions sub"
            " JOIN sub_sources s ON s.sub_id = sub.id"
            " WHERE sub.enabled=1 AND s.source=? ORDER BY sub.id", (channel,))
        return [self._with_children(row) for row in rows]

    # --------------------------------------------- judgments (channel-level judgment cache)
    def save_judgment(self, channel: str, post_id: int, payload: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO judgments(channel, post_id, payload, ts)"
            " VALUES(?,?,?,?)",
            (channel, int(post_id), json.dumps(payload, ensure_ascii=False), _now()))

    def judgments_for(self, channel: str, post_ids: list[int]) -> dict[int, dict]:
        """Fetch judgment results in bulk: {post_id: {template fingerprint: {local question: answer}}}; missing entries are absent from the result."""
        if not post_ids:
            return {}
        marks = ",".join("?" * len(post_ids))
        rows = self._query(
            f"SELECT post_id, payload FROM judgments WHERE channel=? AND post_id IN ({marks})",
            (channel, *post_ids))
        result = {}
        for row in rows:
            try:
                result[row["post_id"]] = json.loads(row["payload"])
            except ValueError:
                continue
        return result

    def prune_judgments(self, days: int = 7) -> int:
        """Prune expired judgment cache entries; returns the number of deleted rows."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self._run("DELETE FROM judgments WHERE ts < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    # -------------------------------------------------------------- logs
    def log(self, sub_id: int | None, kind: str, detail: str = "") -> None:
        self._run("INSERT INTO logs(sub_id, ts, kind, detail) VALUES(?,?,?,?)",
                  (sub_id, _now(), kind, detail[:2000]))

    # ---------------------------------------------------------- internals
    def _run(self, sql: str, args: tuple):
        with self._lock:
            cursor = self._conn.execute(sql, args)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, args).fetchall()]
