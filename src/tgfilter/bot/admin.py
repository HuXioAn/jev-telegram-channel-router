"""管理员命令（bot owner 后台）：用量统计、用户状态与配额管理。

仅 .env 中 ADMIN_USER_IDS 指定的用户可用，且只在私聊生效。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..store import month_start
from .handlers import _require_private, _svc

_KIND_ORDER = ("jev", "llm", "run", "fetch", "deliver")
_KIND_LABEL = {"jev": "Jev判定", "llm": "LLM编译", "run": "执行",
               "fetch": "抓取", "deliver": "推送"}

HELP_TEXT = (
    "🛠 管理员命令\n"
    "/admin — 总览（用户/订阅/用量）\n"
    "/admin users [n] — 用户列表（默认 30）\n"
    "/admin user <id> — 用户详情（用量、配额、订阅）\n"
    "/admin usage [days] — 按用户用量汇总（默认 30 天）\n"
    "/admin block <id> 或 unblock <id> — 停用 / 恢复\n"
    "/admin quota <id> sub <n> — 订阅数上限（0=恢复默认）\n"
    "/admin quota <id> jev <n> — 每月 Jev 判定配额（0=不限）\n"
    "/admin note <id> <备注>"
)


def _fmt_counts(counts: dict[str, int]) -> str:
    parts = [f"{_KIND_LABEL.get(k, k)} {counts[k]}"
             for k in _KIND_ORDER if counts.get(k)]
    return " ｜ ".join(parts) if parts else "无"


def _aggregate(rows: list[dict]) -> dict[int, dict[str, int]]:
    agg: dict[int, dict[str, int]] = {}
    for row in rows:
        agg.setdefault(row["user_id"], {})[row["kind"]] = int(row["s"])
    return agg


def _uid(args: list[str], index: int) -> int:
    if len(args) <= index or not args[index].isdigit():
        raise ValueError(f"第 {index + 1} 个参数应为用户 id")
    return int(args[index])


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    svc = _svc(context)
    if user.id not in svc.settings.admin_user_ids:
        await update.effective_message.reply_text("未知命令。发送 /help 查看用法。")
        return
    if not _require_private(update):
        await update.effective_message.reply_text("请在私聊中使用管理员命令。")
        return
    try:
        text = await _dispatch(svc, context, list(context.args or []))
    except ValueError as exc:
        text = f"⚠️ 参数错误：{exc}\n\n{HELP_TEXT}"
    await update.effective_message.reply_text(text)


async def _dispatch(svc, context, args: list[str]) -> str:
    if not args:
        return _overview(svc)
    cmd = args[0].lower()
    if cmd == "users":
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        return _users(svc, limit=min(max(limit, 1), 100))
    if cmd == "user":
        return _user_detail(svc, _uid(args, 1))
    if cmd == "usage":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        return _usage_summary(svc, days=min(max(days, 1), 365))
    if cmd in ("block", "unblock"):
        return await _set_status(svc, context, cmd, _uid(args, 1))
    if cmd == "quota":
        return _set_quota(svc, args)
    if cmd == "note":
        uid = _uid(args, 1)
        note = " ".join(args[2:]).strip()
        if not note:
            raise ValueError("备注内容为空")
        if not svc.store.get_user(uid):
            return f"❌ 没有用户 {uid}"
        svc.store.set_user_fields(uid, note=note[:200])
        return f"✅ 已更新 {uid} 的备注。"
    return HELP_TEXT


def _overview(svc) -> str:
    store = svc.store
    now = datetime.now(timezone.utc)
    users = store.count_users()
    subs = store.count_subscriptions()
    lines = ["🛠 管理员总览", ""]
    lines.append(f"👥 用户 {users['total']}（活跃 {users.get('active', 0)}"
                 f"｜停用 {users.get('blocked', 0)}）")
    lines.append(f"📡 订阅 {subs['total']}（运行 {subs['enabled']}"
                 f"｜暂停 {subs['total'] - subs['enabled']}）")
    lines.append("")
    for label, since in (("今日", now - timedelta(days=1)),
                         ("7 天", now - timedelta(days=7)),
                         ("30 天", now - timedelta(days=30)),
                         ("累计", None)):
        lines.append(f"📊 {label}：{_fmt_counts(store.usage_by_kind(since=since))}")
    agg = _aggregate(store.usage_rows(since=now - timedelta(days=30)))
    ranked = sorted(agg.items(), key=lambda kv: sum(kv[1].values()), reverse=True)[:5]
    if ranked:
        lines.append("")
        lines.append("🏆 30 天用量 Top 5：")
        for uid, counts in ranked:
            user = store.get_user(uid) or {}
            name = f"@{user['username']}" if user.get("username") else ""
            lines.append(f"· {uid} {name} — {_fmt_counts(counts)}")
    return "\n".join(lines)


def _users(svc, limit: int) -> str:
    store = svc.store
    users = store.list_users()
    agg = _aggregate(store.usage_rows(
        since=datetime.now(timezone.utc) - timedelta(days=30)))
    lines = [f"👥 用户列表（共 {len(users)}，显示 {min(limit, len(users))}；用量=30 天）", ""]
    for user in users[:limit]:
        mark = "⛔" if user["status"] == "blocked" else "✅"
        name = f"@{user['username']}" if user["username"] else ""
        subs = store.count_subscriptions_for(user["id"])
        lines.append(f"{mark} {user['id']} {name}｜订阅 {subs}"
                     f"｜{_fmt_counts(agg.get(user['id'], {}))}")
    if not users:
        lines.append("（暂无用户）")
    return "\n".join(lines)


def _user_detail(svc, uid: int) -> str:
    store = svc.store
    user = store.get_user(uid)
    if not user:
        return f"❌ 没有用户 {uid}"
    now = datetime.now(timezone.utc)
    lines = [f"👤 用户 {uid}" + (f" @{user['username']}" if user["username"] else "")]
    lines.append(f"状态：{'⛔ 已停用' if user['status'] == 'blocked' else '✅ 活跃'}"
                 f"｜注册：{str(user['created_at'])[:10]}")
    lines.append(f"配额：订阅上限 {user['max_subs'] or '默认'}"
                 f"｜月 Jev 判定 {user['quota_jev_monthly'] or '不限'}")
    if user["quota_jev_monthly"]:
        used = store.usage_sum(user_id=uid, kind="jev", since=month_start(now))
        lines.append(f"本月 Jev 已用：{used} / {user['quota_jev_monthly']}")
    if user["note"]:
        lines.append(f"备注：{user['note']}")
    subs = store.list_subscriptions(user_id=uid)
    lines.append("")
    lines.append(f"📡 订阅（{len(subs)}）：")
    for sub in subs[:10]:
        mark = "✅" if sub["enabled"] else "⏸"
        lines.append(f"· #{sub['id']} {mark} @{sub['source']} → "
                     f"{sub['dest_title'] or sub['dest_chat_id']}"
                     f"｜每 {sub['interval_minutes']} 分钟")
    if not subs:
        lines.append("（无）")
    lines.append("")
    for label, since in (("今日", now - timedelta(days=1)),
                         ("7 天", now - timedelta(days=7)),
                         ("30 天", now - timedelta(days=30)),
                         ("累计", None)):
        lines.append(f"📊 {label}：{_fmt_counts(store.usage_by_kind(user_id=uid, since=since))}")
    events = store.recent_usage(user_id=uid, limit=8)
    if events:
        lines.append("")
        lines.append("🕘 最近记录：")
        for event in events:
            ts = str(event["ts"])[5:16].replace("T", " ")
            ref = f" #{event['sub_id']}" if event["sub_id"] else ""
            detail = f"（{event['detail']}）" if event["detail"] else ""
            label = _KIND_LABEL.get(event["kind"], event["kind"])
            lines.append(f"· {ts} {label}×{event['qty']}{ref}{detail}")
    return "\n".join(lines)


def _usage_summary(svc, days: int) -> str:
    store = svc.store
    since = datetime.now(timezone.utc) - timedelta(days=days)
    agg = _aggregate(store.usage_rows(since=since))
    ranked = sorted(agg.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
    total = store.usage_by_kind(since=since)
    lines = [f"📊 用量汇总（最近 {days} 天｜用户 {len(ranked)}"
             f"｜合计 {_fmt_counts(total)}）", ""]
    for uid, counts in ranked[:20]:
        user = store.get_user(uid) or {}
        name = f"@{user['username']}" if user.get("username") else ""
        lines.append(f"· {uid} {name} — {_fmt_counts(counts)}")
    if not ranked:
        lines.append("（无记录）")
    return "\n".join(lines)


async def _set_status(svc, context, action: str, uid: int) -> str:
    if uid in svc.settings.admin_user_ids:
        return "❌ 不能对管理员账号执行该操作。"
    user = svc.store.get_user(uid)
    if not user:
        return f"❌ 没有用户 {uid}"
    if action == "block":
        svc.store.set_user_fields(uid, status="blocked")
        paused = 0
        for sub in svc.store.list_subscriptions(user_id=uid):
            if sub["enabled"]:
                svc.store.set_subscription(sub["id"], enabled=0)
                paused += 1
        try:
            await context.bot.send_message(
                uid, "⛔ 你的使用权限已被管理员暂停；如有疑问请联系管理员。")
        except TelegramError:
            pass
        return f"⛔ 已停用 {uid}（自动暂停 {paused} 个订阅，已尝试通知本人）。"
    svc.store.set_user_fields(uid, status="active")
    return f"✅ 已恢复 {uid} 的使用权限；其订阅仍保持暂停，可在 /list 中恢复。"


def _set_quota(svc, args: list[str]) -> str:
    uid = _uid(args, 1)
    if len(args) < 4 or args[2] not in ("sub", "jev") or not args[3].isdigit():
        raise ValueError("用法：/admin quota <id> sub|jev <n>")
    value = int(args[3])
    if not svc.store.get_user(uid):
        return f"❌ 没有用户 {uid}"
    if args[2] == "sub":
        svc.store.set_user_fields(uid, max_subs=value)
        return f"✅ 已设置 {uid}：订阅数上限 = {value or '默认'}。"
    svc.store.set_user_fields(uid, quota_jev_monthly=value)
    return f"✅ 已设置 {uid}：每月 Jev 判定配额 = {value or '不限'}。"
