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
from .keyboards import dest_kb, interval_kb, main_menu_kb, sub_actions_kb, template_kb

logger = logging.getLogger(__name__)

WAIT_SOURCE, WAIT_DESCRIBE, CONFIRM_TEMPLATE, WAIT_ADJUST, WAIT_DEST, WAIT_INTERVAL = range(6)

K_SOURCE = "pending_source"
K_DESC = "pending_desc"
K_TEMPLATE = "pending_template"
K_HEAD = "pending_head"


def _svc(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["services"]


def _pending_template(context: ContextTypes.DEFAULT_TYPE) -> Template:
    return Template.model_validate(context.user_data[K_TEMPLATE])


async def _edit(query, text: str, reply_markup=None) -> None:
    """编辑消息；内容与现状完全一致时 Telegram 会报 'not modified'，静默忽略。"""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise


# ------------------------------------------------------------------ 基础命令
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    _svc(context).store.add_user(user.id, user.username or "")
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
    context.user_data.clear()
    await update.effective_message.reply_text(msg.ASK_SOURCE)
    return WAIT_SOURCE


async def on_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.effective_message.text or ""
    try:
        channel = normalize_channel_ref(raw)
    except ValueError as exc:
        await update.effective_message.reply_text(msg.BAD_SOURCE.format(err=exc))
        return WAIT_SOURCE
    try:
        info = await _svc(context).fetcher.head(channel)
    except ChannelError as exc:
        await update.effective_message.reply_text(msg.SOURCE_UNREACHABLE.format(err=exc))
        return WAIT_SOURCE
    context.user_data[K_SOURCE] = channel
    context.user_data[K_HEAD] = info.head_id
    sample = (info.posts[-1].text.replace("\n", " ") or "（无文本消息）")[:60]
    await update.effective_message.reply_text(
        msg.SOURCE_OK.format(title=info.title, channel=channel,
                             head=info.head_id, sample=sample)
        + "\n\n" + msg.ASK_DESCRIBE)
    return WAIT_DESCRIBE


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
            template = await svc.compiler.compile(text)
        except LLMError as exc:
            await update.effective_message.reply_text(
                msg.COMPILE_FAILED.format(err=str(exc)[:300]))
            return WAIT_DESCRIBE
    context.user_data[K_DESC] = text
    context.user_data[K_TEMPLATE] = template.model_dump()
    await update.effective_message.reply_text(
        msg.TEMPLATE_CONFIRM.format(summary=template_summary(template)),
        reply_markup=template_kb())
    return CONFIRM_TEMPLATE


async def on_template_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "redo":
        await _edit(query, msg.ASK_DESCRIBE)
        return WAIT_DESCRIBE
    if action == "adjust":
        await _edit(query, msg.ASK_ADJUST)
        return WAIT_ADJUST
    chats = _svc(context).store.list_chats(added_by=update.effective_user.id)
    await _edit(query, msg.ASK_DEST, reply_markup=dest_kb(chats))
    return WAIT_DEST


async def on_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    feedback = (update.effective_message.text or "").strip()
    svc = _svc(context)
    if svc.compiler is None:
        await update.effective_message.reply_text(msg.LLM_NOT_CONFIGURED)
        return WAIT_ADJUST
    await update.effective_message.reply_text(msg.COMPILING)
    try:
        template = await svc.compiler.compile(
            context.user_data.get(K_DESC, ""),
            feedback=feedback,
            previous=_pending_template(context))
    except LLMError as exc:
        await update.effective_message.reply_text(msg.COMPILE_FAILED.format(err=str(exc)[:300]))
        return WAIT_ADJUST
    context.user_data[K_TEMPLATE] = template.model_dump()
    await update.effective_message.reply_text(
        msg.TEMPLATE_CONFIRM.format(summary=template_summary(template)),
        reply_markup=template_kb())
    return CONFIRM_TEMPLATE


async def on_dest_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    user = update.effective_user
    data = query.data
    if data == "dst:refresh":
        await _edit(query, 
            msg.ASK_DEST, reply_markup=dest_kb(svc.store.list_chats(added_by=user.id)))
        return WAIT_DEST
    if data == "dst:dm":
        context.user_data["dest_kind"] = "dm"
        context.user_data["dest_chat_id"] = user.id
        context.user_data["dest_title"] = "私聊"
    else:
        chat_id = int(data.split(":", 2)[2])
        chat = svc.store.get_chat(chat_id) or {}
        title = chat.get("title") or str(chat_id)
        member = None
        try:
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
        except TelegramError:
            member = None
        if member is None or member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            await _edit(query, 
                f"{msg.DEST_CHANNEL_INVALID.format(title=title)}\n\n{msg.ASK_DEST}",
                reply_markup=dest_kb(svc.store.list_chats(added_by=user.id)))
            return WAIT_DEST
        context.user_data["dest_kind"] = "channel"
        context.user_data["dest_chat_id"] = chat_id
        context.user_data["dest_title"] = title
    await _edit(query, msg.ASK_INTERVAL, reply_markup=interval_kb())
    return WAIT_INTERVAL


async def on_interval_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    minutes = int(query.data.split(":", 1)[1])
    svc = _svc(context)
    template = _pending_template(context)
    source = context.user_data[K_SOURCE]
    sub_id = svc.store.add_subscription(
        user_id=update.effective_user.id,
        source=source,
        template=template,
        dest_kind=context.user_data["dest_kind"],
        dest_chat_id=context.user_data["dest_chat_id"],
        dest_title=context.user_data["dest_title"],
        interval_minutes=minutes,
        last_seen_id=context.user_data.get(K_HEAD),
    )
    await _edit(query, msg.SUB_CREATED.format(
        sub_id=sub_id, source=source, rule=match_summary(template),
        dest=context.user_data["dest_title"], interval=minutes))
    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------- 订阅管理
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    user_id = update.effective_user.id
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


# ---------------------------------------------------------------- 错误兜底
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("handler error", exc_info=context.error)
    message = update.effective_message if isinstance(update, Update) else None
    if message is not None:
        try:
            await message.reply_text(msg.ERROR.format(err=str(context.error)[:200]))
        except TelegramError:
            pass
