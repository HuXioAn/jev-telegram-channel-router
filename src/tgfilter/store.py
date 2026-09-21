"""SQLite 存储层：users / chats / subscriptions / logs。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Template

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    max_subs INTEGER,
    quota_jev_monthly INTEGER,
    note TEXT,
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
    source TEXT NOT NULL,
    template_json TEXT NOT NULL,
    dest_kind TEXT NOT NULL,
    dest_chat_id INTEGER NOT NULL,
    dest_title TEXT,
    interval_minutes INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen_id INTEGER,
    last_run_at TEXT,
    created_at TEXT NOT NULL
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def month_start(now: datetime | None = None) -> datetime:
    """当月起点（UTC），按月统计配额用。"""
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
    """把订阅行里的 template_json 解析为 Template。"""
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

    def _migrate(self) -> None:
        """旧库平滑升级：补齐后续新增的列。"""
        additions = {
            "users": {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "max_subs": "INTEGER",
                "quota_jev_monthly": "INTEGER",
                "note": "TEXT",
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

    # ------------------------------------------------------ 统计 / 用量
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
        """记录一条用量：kind ∈ jev/llm/run/fetch/deliver；token 用量为 API 真实值。"""
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
        """按 kind 汇总：{"jev": {"count": n, "in": tokens, "out": tokens}, ...}。"""
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
        """按 (user_id, kind) 汇总（含 token），供管理员视图聚合。"""
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
    def add_subscription(self, *, user_id: int, source: str, template: Template,
                         dest_kind: str, dest_chat_id: int, dest_title: str,
                         interval_minutes: int, last_seen_id: int | None) -> int:
        cursor = self._run(
            "INSERT INTO subscriptions(user_id, source, template_json, dest_kind, "
            "dest_chat_id, dest_title, interval_minutes, enabled, last_seen_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,1,?,?)",
            (user_id, source, template.model_dump_json(), dest_kind, dest_chat_id,
             dest_title, interval_minutes, last_seen_id, _now()))
        return int(cursor.lastrowid or 0)

    def list_subscriptions(self, user_id: int | None = None) -> list[dict]:
        if user_id is None:
            return self._query("SELECT * FROM subscriptions ORDER BY id")
        return self._query("SELECT * FROM subscriptions WHERE user_id=? ORDER BY id", (user_id,))

    def get_subscription(self, sub_id: int) -> dict | None:
        rows = self._query("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
        return rows[0] if rows else None

    def set_subscription(self, sub_id: int, **fields) -> None:
        allowed = {"source", "dest_kind", "dest_chat_id", "dest_title",
                   "interval_minutes", "enabled", "last_seen_id", "last_run_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        clause = ", ".join(f"{key}=?" for key in updates)
        self._run(f"UPDATE subscriptions SET {clause} WHERE id=?",
                  (*updates.values(), sub_id))

    def delete_subscription(self, sub_id: int) -> None:
        self._run("DELETE FROM subscriptions WHERE id=?", (sub_id,))

    def due_subscriptions(self, now: datetime) -> list[dict]:
        """到期（enabled 且距上次运行超过 interval）的订阅。"""
        due, rows = [], self._query("SELECT * FROM subscriptions WHERE enabled=1")
        for row in rows:
            last = _parse_ts(row["last_run_at"])
            if last is None or now - last >= timedelta(minutes=row["interval_minutes"]):
                due.append(row)
        return due

    def mark_run(self, sub_id: int, last_seen_id: int | None, when: str | None = None) -> None:
        self._run("UPDATE subscriptions SET last_seen_id=?, last_run_at=? WHERE id=?",
                  (last_seen_id, when or _now(), sub_id))

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
