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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- users
    def add_user(self, user_id: int, username: str = "") -> None:
        self._run(
            "INSERT INTO users(id, username, created_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
            (user_id, username, _now()))

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
