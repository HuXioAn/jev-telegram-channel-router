"""命令、向导与回调处理器。"""
from __future__ import annotations

import logging

from telegram import ChatMember, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

from . import messages as msg
from ..channel_fetch import ChannelError, normalize_channel_ref
from ..formatting import match_summary, template_summary
from ..llm import LLMError
from ..models import Template
from ..store import template_of
from .keyboards import (dest_manager_kb, edit_interval_kb, edit_menu_kb, interval_kb,
                        main_menu_kb, src_manager_kb, sub_actions_kb, template_kb)

logger = logging.getLogger(__name__)

WAIT_SOURCE, WAIT_DESCRIBE, CONFIRM_TEMPLATE, WAIT_ADJUST, WAIT_DEST, WAIT_INTERVAL = range(6)

K_DESC = "pending_desc"
K_TEMPLATE = "pending_template"
K_EDIT_SUB = "edit_sub_id"   # 编辑流程：正在修改的订阅编号
K_SRC_CTX = "src_ctx"        # 编辑流程：正在添加源频道的订阅编号

MAX_SUBS_PER_USER = 20  # 每用户订阅数上限（防滥用配额）


def _svc(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["services"]


def _require_private(update: Update) -> bool:
    """私聊专用入口守卫：列表/试跑含用户私有数据，避免在群里泄露。"""
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


def _blocked(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """用户是否已被管理员停用（见 /admin block）。"""
    user = _svc(context).store.get_user(user_id) or {}
    return user.get("status") == "blocked"


def _record_llm(svc, user_id: int, usage: dict, detail: str) -> None:
    """记录一次模板编译的真实用量：qty=API 调用次数，另计 input/output token。"""
    svc.store.record_usage(
        user_id, "llm", int(usage.get("calls") or 1), detail=detail,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0))


def resolve_dest_chat(store, user_id: int, chat_id: int) -> dict | None:
    """目的地频道归属校验：只有把机器人添加进该频道的人才能选它。"""
    chat = store.get_chat(chat_id)
    if chat is None or chat.get("added_by") != user_id:
        return None
    return chat


def _owned_sub(svc, user_id: int, raw_id) -> dict | None:
    """按编号取订阅并复核归属（多用户隔离）。"""
    try:
        sub_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    sub = svc.store.get_subscription(sub_id)
    return sub if sub and sub["user_id"] == user_id else None


def _pending_template(context: ContextTypes.DEFAULT_TYPE) -> Template:
    return Template.model_validate(context.user_data[K_TEMPLATE])


def _src_entries(sub: dict) -> list[tuple[str, str]]:
    return [(item["source"], str(item["id"])) for item in sub["sources"]]


def _dest_entries(sub: dict) -> list[tuple[str, str]]:
    return [(item["title"] or str(item["chat_id"]), str(item["id"])) for item in sub["dests"]]


def _dest_text_new(dests: list[dict]) -> str:
    return msg.dest_manager_text([d["title"] or str(d["chat_id"]) for d in dests],
                                 next_step=True)


def _dest_kb_new(svc, user_id: int, dests: list[dict]):
    entries = [(d["title"] or str(d["chat_id"]), str(i)) for i, d in enumerate(dests)]
    selected = {d["chat_id"] for d in dests}
    chats = [c for c in svc.store.list_chats(added_by=user_id) if c["chat_id"] not in selected]
    return dest_manager_kb("new", entries, chats)


def _dest_text_edit(sub: dict) -> str:
    return msg.dest_manager_text([d["title"] or str(d["chat_id"]) for d in sub["dests"]],
                                 next_step=False)


def _dest_kb_edit(svc, user_id: int, sub: dict):
    entries = _dest_entries(sub)
    selected = {d["chat_id"] for d in sub["dests"]}
    chats = [c for c in svc.store.list_chats(added_by=user_id) if c["chat_id"] not in selected]
    return dest_manager_kb(str(sub["id"]), entries, chats)


async def _edit(query, text: str, reply_markup=None) -> None:
    """编辑消息；内容与现状完全一致时 Telegram 会报 'not modified'，静默忽略。"""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _channel_dest_ok(svc, context: ContextTypes.DEFAULT_TYPE,
                           user_id: int, chat_id: int) -> tuple[bool, str]:
    """频道作目的地：归属 + 机器人是管理员 + 用户仍是管理员，三重校验。"""
    title = (svc.store.get_chat(chat_id) or {}).get("title") or str(chat_id)
    if resolve_dest_chat(svc.store, user_id, chat_id) is None:
        return False, msg.DEST_CHANNEL_NOT_YOURS.format(title=title)
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        bot_member = None
    if bot_member is None or bot_member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        return False, msg.DEST_CHANNEL_INVALID.format(title=title)
    try:
        user_member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        user_member = None
    if user_member is None or user_member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        return False, msg.DEST_USER_NOT_ADMIN.format(title=title)
    return True, title


# ------------------------------------------------------------------ 兜底与观测
async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊里未匹配的文本/未知命令：回固定提示，避免“机器人没反应”的错觉。"""
    if update.effective_message:
        await update.effective_message.reply_text(msg.FALLBACK)


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """每条更新记一行日志（独立分组）：用于排查“消息是否到底到达”。"""
    user = update.effective_user
    logger.info("update #%s from %s", update.update_id, user.id if user else "-")


# ------------------------------------------------------------------ 基础命令
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    svc = _svc(context)
    if _blocked(context, user.id):
        await update.effective_message.reply_text(msg.BLOCKED)
        return
    svc.store.add_user(user.id, user.username or "", svc.settings.default_user_status)
    await update.effective_message.reply_text(msg.WELCOME, reply_markup=main_menu_kb())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.HELP)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(msg.CANCELED)
    return ConversationHandler.END


# ---------------------------------------------------------------- /new 向导
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    settings = _svc(context).settings
    if not settings.typesafe_api_key:
        await update.effective_message.reply_text(msg.UNCONFIGURED)
        return ConversationHandler.END
    chat = update.effective_chat
    if chat is not None and chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("请在私聊中使用 /new 创建订阅。")
        return ConversationHandler.END
    if _blocked(context, update.effective_user.id):
        await update.effective_message.reply_text(msg.BLOCKED)
        return ConversationHandler.END
    _svc(context).store.add_user(update.effective_user.id,
                                 update.effective_user.username or "",
                                 settings.default_user_status)
    context.user_data.clear()
    await update.effective_message.reply_text(msg.ASK_SOURCE)
    return WAIT_SOURCE


async def on_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """向导/编辑：收到频道引用 → 验证并在源管理器中登记。"""
    raw = update.effective_message.text or ""
    try:
        channel = normalize_channel_ref(raw)
    except ValueError as exc:
        await update.effective_message.reply_text(msg.BAD_SOURCE.format(err=exc))
        return WAIT_SOURCE
    svc = _svc(context)
    try:
        info = await svc.fetcher.head(channel)
    except ChannelError as exc:
        await update.effective_message.reply_text(msg.SOURCE_UNREACHABLE.format(err=exc))
        return WAIT_SOURCE

    ctx = context.user_data.get(K_SRC_CTX)
    if ctx:  # 编辑模式：给已有订阅追加源频道
        sub = _owned_sub(svc, update.effective_user.id, ctx)
        if sub is None:
            await update.effective_message.reply_text(
                msg.TEST_SUB_NOT_FOUND.format(sub_id=ctx))
            return ConversationHandler.END
        if any(item["source"] == channel for item in sub["sources"]):
            await update.effective_message.reply_text(msg.SOURCE_DUPLICATE)
        else:
            svc.store.add_sub_source(sub["id"], channel, info.head_id)
            sub = svc.store.get_subscription(sub["id"]) or sub
        await update.effective_message.reply_text(
            msg.src_manager_text([item["source"] for item in sub["sources"]],
                                 next_step=False),
            reply_markup=src_manager_kb(str(sub["id"]), _src_entries(sub)))
        return WAIT_SOURCE

    sources = context.user_data.setdefault("sources", [])
    if any(item["source"] == channel for item in sources):
        await update.effective_message.reply_text(msg.SOURCE_DUPLICATE)
    else:
        sources.append({"source": channel, "head_id": info.head_id})
    entries = [(item["source"], str(i)) for i, item in enumerate(sources)]
    await update.effective_message.reply_text(
        msg.src_manager_text([item["source"] for item in sources], next_step=True),
        reply_markup=src_manager_kb("new", entries))
    return WAIT_SOURCE


async def on_src_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """源频道管理器（ms:menu / ms:rm / ms:done；ctx = "new" 或订阅编号）。"""
    query = update.callback_query
    svc = _svc(context)
    user_id = update.effective_user.id
    _, op, ctx, *rest = query.data.split(":")

    if ctx == "new":
        sources = context.user_data.setdefault("sources", [])
        if op == "rm":
            if len(sources) <= 1:
                await query.answer(msg.NEED_ONE_SOURCE, show_alert=True)
                return WAIT_SOURCE
            sources.pop(int(rest[0]))
        elif op == "done":
            if not sources:
                await query.answer(msg.NEED_ONE_SOURCE, show_alert=True)
                return WAIT_SOURCE
            await query.answer()
            await _edit(query, msg.ASK_DESCRIBE)
            return WAIT_DESCRIBE
        await query.answer()
        entries = [(item["source"], str(i)) for i, item in enumerate(sources)]
        await _edit(query,
                    msg.src_manager_text([item["source"] for item in sources],
                                         next_step=True),
                    reply_markup=src_manager_kb("new", entries))
        return WAIT_SOURCE

    in_conv = context.user_data.get(K_SRC_CTX) == ctx
    sub = _owned_sub(svc, user_id, ctx)
    if sub is None:
        await query.answer()
        await _edit(query, msg.TEST_SUB_NOT_FOUND.format(sub_id=ctx))
        return ConversationHandler.END
    if op == "rm":
        if len(sub["sources"]) <= 1:
            await query.answer(msg.NEED_ONE_SOURCE, show_alert=True)
            return WAIT_SOURCE if in_conv else ConversationHandler.END
        svc.store.remove_sub_source(int(rest[0]))
        sub = svc.store.get_subscription(sub["id"]) or sub
    elif op == "done":
        await query.answer()
        context.user_data.pop(K_SRC_CTX, None)
        await _edit(query, msg.edit_menu_text(sub, template_of(sub)),
                    reply_markup=edit_menu_kb(sub["id"]))
        return ConversationHandler.END
    await query.answer()
    await _edit(query,
                msg.src_manager_text([item["source"] for item in sub["sources"]],
                                     next_step=False),
                reply_markup=src_manager_kb(str(sub["id"]), _src_entries(sub)))
    return WAIT_SOURCE if in_conv else ConversationHandler.END


async def on_src_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """编辑订阅：进入「添加源频道」输入流程。"""
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    _, _, raw_id = query.data.split(":")
    sub = _owned_sub(svc, update.effective_user.id, raw_id)
    if sub is None:
        await _edit(query, msg.TEST_SUB_NOT_FOUND.format(sub_id=raw_id))
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data[K_SRC_CTX] = str(sub["id"])
    await _edit(query, msg.EDIT_SOURCE_ASK)
    return WAIT_SOURCE


async def on_describe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    svc = _svc(context)
    if text.startswith("{"):  # Power Mode：直接粘贴模板 JSON
        try:
            template = Template.model_validate_json(text)
        except ValueError as exc:
            await update.effective_message.reply_text(
                msg.BAD_TEMPLATE_JSON.format(err=str(exc)[:300]))
            return WAIT_DESCRIBE
    else:
        if svc.compiler is None:
            await update.effective_message.reply_text(msg.LLM_NOT_CONFIGURED)
            return WAIT_DESCRIBE
        await update.effective_message.reply_text(msg.COMPILING)
        try:
            template, llm_usage = await svc.compiler.compile(text)
        except LLMError as exc:
            _record_llm(svc, update.effective_user.id,
                        getattr(exc, "usage", {}) or {}, "failed")
            await update.effective_message.reply_text(
                msg.COMPILE_FAILED.format(err=str(exc)[:300]))
            return WAIT_DESCRIBE
        _record_llm(svc, update.effective_user.id, llm_usage, text[:60])
    context.user_data[K_DESC] = text
    context.user_data[K_TEMPLATE] = template.model_dump()
    confirm = msg.TEMPLATE_CONFIRM_EDIT if K_EDIT_SUB in context.user_data \
        else msg.TEMPLATE_CONFIRM
    await update.effective_message.reply_text(
        confirm.format(summary=template_summary(template)),
        reply_markup=template_kb())
    return CONFIRM_TEMPLATE


async def on_template_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    editing = context.user_data.get(K_EDIT_SUB)
    if action == "redo":
        await _edit(query, msg.EDIT_TEMPLATE_ASK if editing else msg.ASK_DESCRIBE)
        return WAIT_DESCRIBE
    if action == "adjust":
        await _edit(query, msg.ASK_ADJUST)
        return WAIT_ADJUST
    svc = _svc(context)
    template = _pending_template(context)
    if editing:  # 编辑模式：确认即覆盖保存
        svc.store.set_subscription(int(editing), template_json=template.model_dump_json())
        context.user_data.clear()
        sub = svc.store.get_subscription(int(editing))
        if sub is not None:
            await _edit(query, f"{msg.EDIT_DONE}\n\n{msg.sub_line(sub, template)}",
                        reply_markup=sub_actions_kb(sub))
        else:
            await _edit(query, msg.EDIT_DONE)
        return ConversationHandler.END
    context.user_data.setdefault("dests", [])
    dests = context.user_data["dests"]
    await _edit(query, _dest_text_new(dests),
                reply_markup=_dest_kb_new(svc, update.effective_user.id, dests))
    return WAIT_DEST


async def on_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    feedback = (update.effective_message.text or "").strip()
    svc = _svc(context)
    if svc.compiler is None:
        await update.effective_message.reply_text(msg.LLM_NOT_CONFIGURED)
        return WAIT_ADJUST
    await update.effective_message.reply_text(msg.COMPILING)
    try:
        template, llm_usage = await svc.compiler.compile(
            context.user_data.get(K_DESC, ""),
            feedback=feedback,
            previous=_pending_template(context))
    except LLMError as exc:
        _record_llm(svc, update.effective_user.id,
                    getattr(exc, "usage", {}) or {}, "failed")
        await update.effective_message.reply_text(msg.COMPILE_FAILED.format(err=str(exc)[:300]))
        return WAIT_ADJUST
    _record_llm(svc, update.effective_user.id, llm_usage, feedback[:60])
    context.user_data[K_TEMPLATE] = template.model_dump()
    confirm = msg.TEMPLATE_CONFIRM_EDIT if K_EDIT_SUB in context.user_data \
        else msg.TEMPLATE_CONFIRM
    await update.effective_message.reply_text(
        confirm.format(summary=template_summary(template)),
        reply_markup=template_kb())
    return CONFIRM_TEMPLATE


async def on_dest_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """目的地管理器（md:menu / md:rm / md:dm / md:ch / md:refresh / md:done）。"""
    query = update.callback_query
    svc = _svc(context)
    user = update.effective_user
    _, op, ctx, *rest = query.data.split(":")

    if ctx == "new":
        dests = context.user_data.setdefault("dests", [])
        if op == "rm":
            if len(dests) <= 1:
                await query.answer(msg.NEED_ONE_DEST, show_alert=True)
                return WAIT_DEST
            dests.pop(int(rest[0]))
        elif op == "dm":
            if any(d["chat_id"] == user.id for d in dests):
                await query.answer(msg.DEST_DUPLICATE, show_alert=True)
                return WAIT_DEST
            dests.append({"kind": "dm", "chat_id": user.id, "title": "私聊"})
        elif op == "ch":
            chat_id = int(rest[0])
            if any(d["chat_id"] == chat_id for d in dests):
                await query.answer(msg.DEST_DUPLICATE, show_alert=True)
                return WAIT_DEST
            ok, title = await _channel_dest_ok(svc, context, user.id, chat_id)
            if not ok:
                await query.answer()
                await _edit(query, f"{title}\n\n{_dest_text_new(dests)}",
                            reply_markup=_dest_kb_new(svc, user.id, dests))
                return WAIT_DEST
            dests.append({"kind": "channel", "chat_id": chat_id, "title": title})
        elif op == "done":
            if not dests:
                await query.answer(msg.NEED_ONE_DEST, show_alert=True)
                return WAIT_DEST
            await query.answer()
            await _edit(query, msg.ASK_INTERVAL, reply_markup=interval_kb())
            return WAIT_INTERVAL
        await query.answer("已刷新" if op == "refresh" else None)
        await _edit(query, _dest_text_new(dests),
                    reply_markup=_dest_kb_new(svc, user.id, dests))
        return WAIT_DEST

    sub = _owned_sub(svc, user.id, ctx)
    if sub is None:
        await query.answer()
        await _edit(query, msg.TEST_SUB_NOT_FOUND.format(sub_id=ctx))
        return ConversationHandler.END
    if op == "rm":
        if len(sub["dests"]) <= 1:
            await query.answer(msg.NEED_ONE_DEST, show_alert=True)
            return ConversationHandler.END
        svc.store.remove_sub_dest(int(rest[0]))
    elif op == "dm":
        if any(d["chat_id"] == user.id for d in sub["dests"]):
            await query.answer(msg.DEST_DUPLICATE, show_alert=True)
            return ConversationHandler.END
        svc.store.add_sub_dest(sub["id"], "dm", user.id, "私聊")
    elif op == "ch":
        chat_id = int(rest[0])
        if any(d["chat_id"] == chat_id for d in sub["dests"]):
            await query.answer(msg.DEST_DUPLICATE, show_alert=True)
            return ConversationHandler.END
        ok, title = await _channel_dest_ok(svc, context, user.id, chat_id)
        if not ok:
            await query.answer()
            await _edit(query, f"{title}\n\n{_dest_text_edit(sub)}",
                        reply_markup=_dest_kb_edit(svc, user.id, sub))
            return ConversationHandler.END
        svc.store.add_sub_dest(sub["id"], "channel", chat_id, title)
    elif op == "done":
        await query.answer()
        await _edit(query, msg.edit_menu_text(sub, template_of(sub)),
                    reply_markup=edit_menu_kb(sub["id"]))
        return ConversationHandler.END
    await query.answer("已刷新" if op == "refresh" else None)
    sub = svc.store.get_subscription(sub["id"]) or sub
    await _edit(query, _dest_text_edit(sub), reply_markup=_dest_kb_edit(svc, user.id, sub))
    return ConversationHandler.END


async def on_interval_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    minutes = int(query.data.split(":", 1)[1])
    svc = _svc(context)
    user = update.effective_user
    cap = (svc.store.get_user(user.id) or {}).get("max_subs") or MAX_SUBS_PER_USER
    if len(svc.store.list_subscriptions(user_id=user.id)) >= cap:
        await query.answer()
        await _edit(query, msg.SUBS_LIMIT.format(n=cap))
        context.user_data.clear()
        return ConversationHandler.END
    sources = context.user_data.get("sources") or []
    dests = context.user_data.get("dests") or []
    if not sources:
        await query.answer()
        await _edit(query, msg.NEED_ONE_SOURCE)
        context.user_data.clear()
        return ConversationHandler.END
    if not dests:
        await query.answer()
        await _edit(query, _dest_text_new(dests),
                    reply_markup=_dest_kb_new(svc, user.id, dests))
        return WAIT_DEST
    await query.answer()
    template = _pending_template(context)
    sub_id = svc.store.add_subscription(
        user_id=user.id, template=template, interval_minutes=minutes,
        sources=[{"source": item["source"], "last_seen_id": item.get("head_id")}
                 for item in sources],
        dests=dests)
    src_names = "、".join(f"@{item['source']}" for item in sources)
    dest_names = "、".join(d["title"] or str(d["chat_id"]) for d in dests)
    context.user_data.clear()
    await _edit(query, msg.SUB_CREATED.format(
        sub_id=sub_id, sources=src_names, rule=match_summary(template),
        dests=dest_names, interval=minutes))
    return ConversationHandler.END


# ---------------------------------------------------------------- 订阅管理
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_private(update):
        await update.effective_message.reply_text(msg.PRIVATE_ONLY.format(cmd="/list"))
        return
    if _blocked(context, update.effective_user.id):
        await update.effective_message.reply_text(msg.BLOCKED)
        return
    subs = _svc(context).store.list_subscriptions(user_id=update.effective_user.id)
    if not subs:
        await update.effective_message.reply_text(msg.NO_SUBS)
        return
    for sub in subs:
        await update.effective_message.reply_text(
            msg.sub_line(sub, template_of(sub)), reply_markup=sub_actions_kb(sub))


async def on_sub_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    user_id = update.effective_user.id
    _, action, raw_id = query.data.split(":")
    sub_id = int(raw_id)
    sub = svc.store.get_subscription(sub_id)
    if sub is None or sub["user_id"] != user_id:
        await _edit(query, msg.TEST_SUB_NOT_FOUND.format(sub_id=sub_id))
        return
    if action in ("pause", "resume"):
        svc.store.set_subscription(sub_id, enabled=1 if action == "resume" else 0)
        sub = svc.store.get_subscription(sub_id) or sub
        await _edit(query, msg.sub_line(sub, template_of(sub)),
                    reply_markup=sub_actions_kb(sub))
    elif action == "delete":
        svc.store.delete_subscription(sub_id)
        await _edit(query, f"🗑 订阅 #{sub_id} 已删除。")
    elif action == "test":
        await _edit(query, msg.TESTING)
        result = await svc.pipeline.preview(sub, pool=100, limit=6)
        await _edit(query, msg.test_result(result, sub, template_of(sub)))
    elif action == "edit":
        await _edit(query, msg.edit_menu_text(sub, template_of(sub)),
                    reply_markup=edit_menu_kb(sub_id))
    elif action == "show":
        await _edit(query, msg.sub_line(sub, template_of(sub)),
                    reply_markup=sub_actions_kb(sub))


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_private(update):
        await update.effective_message.reply_text(msg.PRIVATE_ONLY.format(cmd="/test"))
        return
    svc = _svc(context)
    user_id = update.effective_user.id
    if _blocked(context, user_id):
        await update.effective_message.reply_text(msg.BLOCKED)
        return
    args = context.args or []
    subs = svc.store.list_subscriptions(user_id=user_id)
    if not subs:
        await update.effective_message.reply_text(msg.NO_SUBS)
        return
    sub = None
    if args and args[0].isdigit():
        sub = svc.store.get_subscription(int(args[0]))
        if sub is not None and sub["user_id"] != user_id:
            sub = None
        if sub is None:
            await update.effective_message.reply_text(
                msg.TEST_SUB_NOT_FOUND.format(sub_id=args[0]))
            return
    elif len(subs) == 1:
        sub = subs[0]
    else:
        await update.effective_message.reply_text(msg.TEST_NEED_ID)
        return
    placeholder = await update.effective_message.reply_text(msg.TESTING)
    result = await svc.pipeline.preview(sub, pool=100, limit=6)
    await placeholder.edit_text(msg.test_result(result, sub, template_of(sub)))


# ---------------------------------------------------------------- 订阅编辑
async def on_edit_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """编辑订阅：频率选择面板。"""
    query = update.callback_query
    await query.answer()
    _, raw_id = query.data.split(":")
    sub = _owned_sub(_svc(context), update.effective_user.id, raw_id)
    if sub is None:
        await _edit(query, msg.TEST_SUB_NOT_FOUND.format(sub_id=raw_id))
        return
    await _edit(query, msg.EDIT_INTERVAL, reply_markup=edit_interval_kb(sub["id"]))


async def on_edit_interval_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """编辑订阅：应用新频率并回到订阅视图。"""
    query = update.callback_query
    await query.answer()
    _, raw_id, raw_minutes = query.data.split(":")
    svc = _svc(context)
    sub = _owned_sub(svc, update.effective_user.id, raw_id)
    if sub is None:
        await _edit(query, msg.TEST_SUB_NOT_FOUND.format(sub_id=raw_id))
        return
    svc.store.set_subscription(sub["id"], interval_minutes=int(raw_minutes))
    sub = svc.store.get_subscription(sub["id"]) or sub
    await _edit(query, f"{msg.EDIT_DONE}\n\n{msg.sub_line(sub, template_of(sub))}",
                reply_markup=sub_actions_kb(sub))


async def on_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """编辑订阅：进入「重新描述筛选条件」流程（确认后覆盖模板）。"""
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    _, raw_id = query.data.split(":")
    sub = _owned_sub(svc, update.effective_user.id, raw_id)
    if sub is None:
        await _edit(query, msg.TEST_SUB_NOT_FOUND.format(sub_id=raw_id))
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data[K_EDIT_SUB] = str(sub["id"])
    context.user_data[K_DESC] = template_of(sub).name or ""
    await _edit(query, msg.EDIT_TEMPLATE_ASK)
    return WAIT_DESCRIBE


async def on_ui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "ui:list":
        await cmd_list(update, context)
    elif query.data == "ui:help":
        await update.effective_message.reply_text(msg.HELP)


# ---------------------------------------------------------------- 频道登记
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if change is None:
        return
    chat = change.chat
    if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    status = change.new_chat_member.status
    store = _svc(context).store
    adminish = status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    can_post = adminish if chat.type == ChatType.CHANNEL else (
        adminish or status == ChatMember.MEMBER)
    if can_post:
        adder = change.from_user.id if change.from_user else None
        store.upsert_chat(chat.id, chat.type, chat.title or "", adder)
        if adder:
            try:
                await context.bot.send_message(adder, msg.CHAT_ADDED.format(
                    title=chat.title or chat.id))
            except TelegramError:
                pass
    elif status in (ChatMember.LEFT, ChatMember.BANNED):
        store.remove_chat(chat.id)
    elif chat.type == ChatType.CHANNEL and status == ChatMember.MEMBER:
        # 频道内被降权（管理员 → 普通成员）：不能再发帖，从可选目的地移除
        store.remove_chat(chat.id)


# ---------------------------------------------------------------- 错误兜底
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("handler error", exc_info=context.error)
    message = update.effective_message if isinstance(update, Update) else None
    if message is not None:
        try:
            await message.reply_text(msg.ERROR.format(err=str(context.error)[:200]))
        except TelegramError:
            pass
