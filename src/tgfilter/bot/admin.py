"""Admin commands (bot owner console): usage stats, user status and quotas.

Only the user ids listed in ADMIN_USER_IDS (.env) may use these, and only in
private chats. All output follows the admin's own UI language.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from . import messages as msg
from ..i18n import LANG_LABELS, t
from ..store import month_start
from .handlers import _lang, _require_private, _svc

_KIND_ORDER = ("jev", "consumed", "llm", "run", "fetch", "deliver")


def _kind_label(lang: str, kind: str) -> str:
    return t(lang, f"kind_{kind}")


def _tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_rollup(lang: str, rollup: dict[str, dict[str, int]]) -> str:
    """Usage summary line: counters for everything; real tokens (in→out) for jev/llm."""
    parts = []
    for kind in _KIND_ORDER:
        item = rollup.get(kind)
        if not item or not item.get("count"):
            continue
        label = _kind_label(lang, kind)
        if item.get("in") or item.get("out"):
            parts.append(f"{label} {item['count']}" + t(
                lang, "tok_suffix", in_tok=_tok(item["in"]), out_tok=_tok(item["out"])))
        else:
            parts.append(f"{label} {item['count']}")
    return " ｜ ".join(parts) if parts else t(lang, "admin_none")


def _aggregate(rows: list[dict]) -> dict[int, dict[str, dict[str, int]]]:
    agg: dict[int, dict[str, dict[str, int]]] = {}
    for row in rows:
        agg.setdefault(row["user_id"], {})[row["kind"]] = {
            "count": int(row["s"]), "in": int(row["tin"]), "out": int(row["tout"])}
    return agg


def _uid(args: list[str], index: int, lang: str) -> int:
    if len(args) <= index or not args[index].isdigit():
        raise ValueError(t(lang, "admin_bad_uid", n=index + 1))
    return int(args[index])


def _uname(store, uid: int, lang: str) -> str:
    """Display name: 0 = the system (channel-shared accounting), otherwise the username."""
    if uid == 0:
        return t(lang, "admin_system_name")
    user = store.get_user(uid) or {}
    return f"@{user['username']}" if user.get("username") else ""


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    svc = _svc(context)
    lang = _lang(svc, user.id)
    if user.id not in svc.settings.admin_user_ids:
        await update.effective_message.reply_text(msg.unknown_cmd(lang))
        return
    if not _require_private(update):
        await update.effective_message.reply_text(t(lang, "admin_private_only"))
        return
    try:
        text = await _dispatch(svc, context, list(context.args or []), lang)
    except ValueError as exc:
        text = t(lang, "admin_param_error", err=exc, help=t(lang, "admin_help"))
    await update.effective_message.reply_text(text)


async def _dispatch(svc, context, args: list[str], lang: str) -> str:
    if not args:
        return _overview(svc, lang)
    cmd = args[0].lower()
    if cmd == "users":
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        return _users(svc, min(max(limit, 1), 100), lang)
    if cmd == "user":
        return _user_detail(svc, _uid(args, 1, lang), lang)
    if cmd == "usage":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        return _usage_summary(svc, min(max(days, 1), 365), lang)
    if cmd == "watches":
        return _watches(svc, lang)
    if cmd == "interval":
        return _interval(svc, args, lang)
    if cmd in ("block", "unblock"):
        return await _set_status(svc, context, cmd, _uid(args, 1, lang), lang)
    if cmd == "quota":
        return _set_quota(svc, args, lang)
    if cmd == "lang":
        return _set_default_lang(svc, args, lang)
    if cmd == "note":
        uid = _uid(args, 1, lang)
        note = " ".join(args[2:]).strip()
        if not note:
            raise ValueError(t(lang, "admin_note_empty"))
        if not svc.store.get_user(uid):
            return t(lang, "admin_user_not_found", uid=uid)
        svc.store.set_user_fields(uid, note=note[:200])
        return t(lang, "admin_note_done", uid=uid)
    return t(lang, "admin_help")


def _current_default_lang(svc) -> str:
    return svc.store.get_setting("default_lang") or svc.settings.default_lang


def _set_default_lang(svc, args: list[str], lang: str) -> str:
    if len(args) < 2 or args[1].lower() not in LANG_LABELS:
        return t(lang, "admin_lang_bad")
    code = args[1].lower()
    svc.store.set_setting("default_lang", code)
    return t(lang, "admin_lang_set", language=LANG_LABELS[code])


def _overview(svc, lang: str) -> str:
    store = svc.store
    now = datetime.now(timezone.utc)
    users = store.count_users()
    subs = store.count_subscriptions()
    lines = [t(lang, "admin_overview_title"), ""]
    lines.append(t(lang, "admin_users_line", total=users["total"],
                    active=users.get("active", 0), blocked=users.get("blocked", 0)))
    lines.append(t(lang, "admin_subs_line", total=subs["total"], enabled=subs["enabled"],
                    paused=subs["total"] - subs["enabled"]))
    lines.append(t(lang, "admin_lang_line", language=LANG_LABELS[_current_default_lang(svc)]))
    lines.append(t(lang, "admin_interval_line",
                    minutes=svc.store.fetch_interval_minutes(
                        svc.settings.default_interval_minutes)))
    lines.append("")
    for label_key, since in (("admin_period_today", now - timedelta(days=1)),
                             ("admin_period_7d", now - timedelta(days=7)),
                             ("admin_period_30d", now - timedelta(days=30)),
                             ("admin_period_all", None)):
        lines.append(t(lang, "admin_usage_line", label=t(lang, label_key),
                        rollup=_fmt_rollup(lang, store.usage_rollup(since=since))))
    agg = _aggregate(store.usage_rows(since=now - timedelta(days=30)))
    ranked = sorted(agg.items(),
                    key=lambda kv: sum(v["count"] for v in kv[1].values()),
                    reverse=True)[:5]
    if ranked:
        lines.append("")
        lines.append(t(lang, "admin_top5_title"))
        for uid, counts in ranked:
            lines.append(f"· {uid} {_uname(store, uid, lang)} — {_fmt_rollup(lang, counts)}")
    return "\n".join(lines)


def _watches(svc, lang: str) -> str:
    """Channel-level refresh schedule (one global interval for every channel)."""
    store = svc.store
    lines = [t(lang, "admin_watch_list_title"), ""]
    rows = store.list_watches()
    for row in rows:
        watchers = len(store.watchers_of(row["channel"]))
        last = (str(row["last_fetch_at"])[5:16].replace("T", " ")
                if row["last_fetch_at"] else t(lang, "admin_watch_never"))
        cursor = row["last_seen_id"] if row["last_seen_id"] is not None else "—"
        lines.append(t(lang, "admin_watch_line", channel=row["channel"],
                        watchers=watchers, cursor=cursor, last=last))
    if not rows:
        lines.append(t(lang, "admin_none"))
    lines.append("")
    lines.append(t(lang, "admin_interval_line",
                    minutes=store.fetch_interval_minutes(
                        svc.settings.default_interval_minutes)))
    return "\n".join(lines)


def _interval(svc, args: list[str], lang: str) -> str:
    """Global refresh interval: show (no args) or set (/admin interval <minutes>)."""
    default = svc.settings.default_interval_minutes
    if len(args) < 2:
        return t(lang, "admin_interval_show",
                 minutes=svc.store.fetch_interval_minutes(default))
    if not args[1].isdigit() or not 1 <= int(args[1]) <= 1440:
        raise ValueError(t(lang, "admin_interval_bad_minutes"))
    minutes = int(args[1])
    svc.store.set_fetch_interval_minutes(minutes)
    return t(lang, "admin_interval_set", minutes=minutes)


def _users(svc, limit: int, lang: str) -> str:
    store = svc.store
    users = store.list_users()
    agg = _aggregate(store.usage_rows(
        since=datetime.now(timezone.utc) - timedelta(days=30)))
    lines = [t(lang, "admin_users_title", n=len(users), shown=min(limit, len(users))), ""]
    for user in users[:limit]:
        mark = "⛔" if user["status"] == "blocked" else "✅"
        name = f"@{user['username']}" if user["username"] else ""
        subs = store.count_subscriptions_for(user["id"])
        lines.append(t(lang, "admin_user_line", mark=mark, uid=user["id"],
                        name=name, subs=subs,
                        rollup=_fmt_rollup(lang, agg.get(user["id"], {}))))
    if not users:
        lines.append(t(lang, "admin_users_none"))
    return "\n".join(lines)


def _user_detail(svc, uid: int, lang: str) -> str:
    store = svc.store
    user = store.get_user(uid)
    if not user:
        return t(lang, "admin_user_not_found", uid=uid)
    now = datetime.now(timezone.utc)
    name = f" @{user['username']}" if user["username"] else ""
    lines = [t(lang, "admin_user_title", uid=uid) + name]
    status = t(lang, "admin_status_blocked" if user["status"] == "blocked"
               else "admin_status_active")
    lines.append(t(lang, "admin_user_status", status=status,
                    created=str(user["created_at"])[:10]))
    lines.append(t(lang, "admin_user_quota",
                    max_subs=user["max_subs"] or t(lang, "admin_default"),
                    quota=user["quota_jev_monthly"] or t(lang, "admin_unlimited")))
    if user["quota_jev_monthly"]:
        used = store.usage_sum(user_id=uid, kind="consumed", since=month_start(now))
        lines.append(t(lang, "admin_consumed_month", used=used,
                        quota=user["quota_jev_monthly"]))
    month_rollup = store.usage_rollup(user_id=uid, since=month_start(now))
    month_in = sum(item["in"] for item in month_rollup.values())
    month_out = sum(item["out"] for item in month_rollup.values())
    if month_in or month_out:
        lines.append(t(lang, "admin_month_tokens", in_tok=f"{month_in:,}",
                        out_tok=f"{month_out:,}"))
    if user["note"]:
        lines.append(t(lang, "admin_note_line", note=user["note"]))
    subs = store.list_subscriptions(user_id=uid)
    lines.append("")
    lines.append(t(lang, "admin_subs_head", n=len(subs)))
    for sub in subs[:10]:
        mark = "✅" if sub["enabled"] else "⏸"
        sep = t(lang, "list_sep")
        sources = sep.join("@" + item["source"] for item in sub["sources"][:3]) \
            or t(lang, "none_label")
        dests = sep.join(item["title"] or str(item["chat_id"])
                         for item in sub["dests"][:3]) or t(lang, "none_label")
        lines.append(t(lang, "admin_sub_line", id=sub["id"], mark=mark,
                        sources=sources, dests=dests))
    if not subs:
        lines.append(t(lang, "admin_none"))
    lines.append("")
    for label_key, since in (("admin_period_today", now - timedelta(days=1)),
                             ("admin_period_7d", now - timedelta(days=7)),
                             ("admin_period_30d", now - timedelta(days=30)),
                             ("admin_period_all", None)):
        lines.append(t(lang, "admin_usage_line", label=t(lang, label_key),
                        rollup=_fmt_rollup(lang,
                                           store.usage_rollup(user_id=uid, since=since))))
    events = store.recent_usage(user_id=uid, limit=8)
    if events:
        lines.append("")
        lines.append(t(lang, "admin_recent_head"))
        for event in events:
            ts = str(event["ts"])[5:16].replace("T", " ")
            ref = f" #{event['sub_id']}" if event["sub_id"] else ""
            detail = t(lang, "paren", text=event["detail"]) if event["detail"] else ""
            label = _kind_label(lang, event["kind"])
            tokens = (t(lang, "tok_suffix", in_tok=event["input_tokens"],
                        out_tok=event["output_tokens"])
                      if event["input_tokens"] or event["output_tokens"] else "")
            lines.append(t(lang, "admin_recent_line", ts=ts, label=label,
                            qty=event["qty"], tokens=tokens, ref=ref, detail=detail))
    return "\n".join(lines)


def _usage_summary(svc, days: int, lang: str) -> str:
    store = svc.store
    since = datetime.now(timezone.utc) - timedelta(days=days)
    agg = _aggregate(store.usage_rows(since=since))
    ranked = sorted(agg.items(),
                    key=lambda kv: sum(v["count"] for v in kv[1].values()),
                    reverse=True)
    total = store.usage_rollup(since=since)
    lines = [t(lang, "admin_usage_title", days=days, users=len(ranked),
               rollup=_fmt_rollup(lang, total)), ""]
    for uid, counts in ranked[:20]:
        lines.append(f"· {uid} {_uname(store, uid, lang)} — {_fmt_rollup(lang, counts)}")
    if not ranked:
        lines.append(t(lang, "admin_no_records"))
    return "\n".join(lines)


async def _set_status(svc, context, action: str, uid: int, lang: str) -> str:
    if uid in svc.settings.admin_user_ids:
        return t(lang, "admin_cannot_target_admin")
    user = svc.store.get_user(uid)
    if not user:
        return t(lang, "admin_user_not_found", uid=uid)
    if action == "block":
        svc.store.set_user_fields(uid, status="blocked")
        paused = 0
        for sub in svc.store.list_subscriptions(user_id=uid):
            if sub["enabled"]:
                svc.store.set_subscription(sub["id"], enabled=0)
                paused += 1
        try:
            target_lang = svc.store.language_for(uid, svc.settings.default_lang)
            await context.bot.send_message(uid, t(target_lang, "admin_block_notice"))
        except TelegramError:
            pass
        return t(lang, "admin_block_done", uid=uid, paused=paused)
    svc.store.set_user_fields(uid, status="active")
    return t(lang, "admin_unblock_done", uid=uid)


def _set_quota(svc, args: list[str], lang: str) -> str:
    uid = _uid(args, 1, lang)
    if len(args) < 4 or args[2] not in ("sub", "jev") or not args[3].isdigit():
        raise ValueError(t(lang, "admin_quota_usage_error"))
    value = int(args[3])
    if not svc.store.get_user(uid):
        return t(lang, "admin_user_not_found", uid=uid)
    if args[2] == "sub":
        svc.store.set_user_fields(uid, max_subs=value)
        return t(lang, "admin_quota_sub_done", uid=uid,
                 value=value or t(lang, "admin_default"))
    svc.store.set_user_fields(uid, quota_jev_monthly=value)
    return t(lang, "admin_quota_jev_done", uid=uid,
             value=value or t(lang, "admin_unlimited"))
