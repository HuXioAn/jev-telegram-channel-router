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

_KIND_ORDER = ("jev", "consumed", "llm", "run", "fetch", "deliver")
_KIND_LABEL = {"jev": "Jev判定", "consumed": "判定消费", "llm": "LLM编译",
               "run": "执行", "fetch": "抓取", "deliver": "推送"}

HELP_TEXT = (
    "🛠 管理员命令\n"
    "/admin — 总览（用户/订阅/用量）\n"
    "/admin users [n] — 用户列表（默认 30）\n"
    "/admin user <id> — 用户详情（用量、配额、订阅）\n"
    "/admin usage [days] — 按用户用量汇总（默认 30 天）\n"
    "/admin watches — 频道刷新调度（间隔/游标/订阅数）\n"
    "/admin watch <频道> <分钟> — 调整某频道刷新间隔\n"
    "/admin block <id> 或 unblock <id> — 停用 / 恢复\n"
    "/admin quota <id> sub <n> — 订阅数上限（0=恢复默认）\n"
    "/admin quota <id> jev <n> — 每月判定配额（0=不限）\n"
    "/admin note <id> <备注>"
)


def _tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_rollup(rollup: dict[str, dict[str, int]]) -> str:
    """用量汇总行：非 token 维度只报次数；jev/llm 附真实 token（in→out）。"""
    parts = []
    for kind in _KIND_ORDER:
        item = rollup.get(kind)
        if not item or not item.get("count"):
            continue
        label = _KIND_LABEL.get(kind, kind)
        if item.get("in") or item.get("out"):
            parts.append(f"{label} {item['count']}"
                         f"（tok {_tok(item['in'])}→{_tok(item['out'])}）")
        else:
            parts.append(f"{label} {item['count']}")
    return " ｜ ".join(parts) if parts else "无"


def _aggregate(rows: list[dict]) -> dict[int, dict[str, dict[str, int]]]:
    agg: dict[int, dict[str, dict[str, int]]] = {}
    for row in rows:
        agg.setdefault(row["user_id"], {})[row["kind"]] = {
            "count": int(row["s"]), "in": int(row["tin"]), "out": int(row["tout"])}
    return agg


def _uid(args: list[str], index: int) -> int:
    if len(args) <= index or not args[index].isdigit():
        raise ValueError(f"第 {index + 1} 个参数应为用户 id")
    return int(args[index])


def _uname(store, uid: int) -> str:
    """展示名：0 = 系统（频道共享记账），其余取用户名。"""
    if uid == 0:
        return "🛰 频道共享（系统）"
    user = store.get_user(uid) or {}
    return f"@{user['username']}" if user.get("username") else ""


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
    if cmd == "watches":
        return _watches(svc)
    if cmd == "watch":
        return _set_watch(svc, args)
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
        lines.append(f"📊 {label}：{_fmt_rollup(store.usage_rollup(since=since))}")
    agg = _aggregate(store.usage_rows(since=now - timedelta(days=30)))
    ranked = sorted(agg.items(),
                    key=lambda kv: sum(v["count"] for v in kv[1].values()),
                    reverse=True)[:5]
    if ranked:
        lines.append("")
        lines.append("🏆 30 天用量 Top 5：")
        for uid, counts in ranked:
            lines.append(f"· {uid} {_uname(store, uid)} — {_fmt_rollup(counts)}")
    return "\n".join(lines)


def _watches(svc) -> str:
    """频道刷新调度列表。"""
    store = svc.store
    lines = ["📡 频道刷新调度", ""]
    rows = store.list_watches()
    for row in rows:
        watchers = len(store.watchers_of(row["channel"]))
        last = (str(row["last_fetch_at"])[5:16].replace("T", " ")
                if row["last_fetch_at"] else "未抓取")
        cursor = row["last_seen_id"] if row["last_seen_id"] is not None else "—"
        lines.append(f"· @{row['channel']}｜每 {row['interval_minutes']} 分钟"
                     f"｜订阅 {watchers}｜游标 {cursor}｜上次 {last}")
    if not rows:
        lines.append("（无）")
    lines.append("")
    lines.append("调整：/admin watch <频道> <分钟>")
    return "\n".join(lines)


def _set_watch(svc, args: list[str]) -> str:
    if len(args) < 3:
        raise ValueError("用法：/admin watch <频道> <分钟>")
    channel = args[1].lstrip("@").strip()
    if not args[2].isdigit() or int(args[2]) < 1:
        raise ValueError("分钟数应为正整数")
    if not svc.store.get_watch(channel):
        return f"❌ 没有在观察的频道 {channel}（用 /admin watches 查看）"
    minutes = int(args[2])
    svc.store.set_watch_interval(channel, minutes)
    return f"✅ 已设置 @{channel}：每 {minutes} 分钟刷新。"


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
                     f"｜{_fmt_rollup(agg.get(user['id'], {}))}")
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
                 f"｜月判定额度 {user['quota_jev_monthly'] or '不限'}")
    if user["quota_jev_monthly"]:
        used = store.usage_sum(user_id=uid, kind="consumed", since=month_start(now))
        lines.append(f"本月判定已消费：{used} / {user['quota_jev_monthly']}")
    month_rollup = store.usage_rollup(user_id=uid, since=month_start(now))
    month_in = sum(item["in"] for item in month_rollup.values())
    month_out = sum(item["out"] for item in month_rollup.values())
    if month_in or month_out:
        lines.append(f"本月 token：in {month_in:,} → out {month_out:,}")
    if user["note"]:
        lines.append(f"备注：{user['note']}")
    subs = store.list_subscriptions(user_id=uid)
    lines.append("")
    lines.append(f"📡 订阅（{len(subs)}）：")
    for sub in subs[:10]:
        mark = "✅" if sub["enabled"] else "⏸"
        sources = "、".join("@" + item["source"] for item in sub["sources"][:3]) or "（无）"
        dests = "、".join(item["title"] or str(item["chat_id"])
                          for item in sub["dests"][:3]) or "（无）"
        lines.append(f"· #{sub['id']} {mark} {sources} → {dests}")
    if not subs:
        lines.append("（无）")
    lines.append("")
    for label, since in (("今日", now - timedelta(days=1)),
                         ("7 天", now - timedelta(days=7)),
                         ("30 天", now - timedelta(days=30)),
                         ("累计", None)):
        lines.append(f"📊 {label}：{_fmt_rollup(store.usage_rollup(user_id=uid, since=since))}")
    events = store.recent_usage(user_id=uid, limit=8)
    if events:
        lines.append("")
        lines.append("🕘 最近记录：")
        for event in events:
            ts = str(event["ts"])[5:16].replace("T", " ")
            ref = f" #{event['sub_id']}" if event["sub_id"] else ""
            detail = f"（{event['detail']}）" if event["detail"] else ""
            label = _KIND_LABEL.get(event["kind"], event["kind"])
            tokens = (f" tok:{event['input_tokens']}→{event['output_tokens']}"
                      if event["input_tokens"] or event["output_tokens"] else "")
            lines.append(f"· {ts} {label}×{event['qty']}{tokens}{ref}{detail}")
    return "\n".join(lines)


def _usage_summary(svc, days: int) -> str:
    store = svc.store
    since = datetime.now(timezone.utc) - timedelta(days=days)
    agg = _aggregate(store.usage_rows(since=since))
    ranked = sorted(agg.items(),
                    key=lambda kv: sum(v["count"] for v in kv[1].values()),
                    reverse=True)
    total = store.usage_rollup(since=since)
    lines = [f"📊 用量汇总（最近 {days} 天｜用户 {len(ranked)}"
             f"｜合计 {_fmt_rollup(total)}）", ""]
    for uid, counts in ranked[:20]:
        lines.append(f"· {uid} {_uname(store, uid)} — {_fmt_rollup(counts)}")
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
