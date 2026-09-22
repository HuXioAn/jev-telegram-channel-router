"""Command, wizard and callback handlers."""
from __future__ import annotations

import logging

from telegram import ChatMember, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

from . import messages as msg
from .keyboards import (back_list_kb, dest_manager_kb, edit_menu_kb, lang_kb,
                        main_menu_kb, src_manager_kb, sub_actions_kb,
                        sub_pick_kb, template_kb)
from .. import i18n
from ..channel_fetch import ChannelError, normalize_channel_ref
from ..formatting import match_summary, template_summary
from ..i18n import LANG_LABELS
from ..llm import LLMError
from ..models import TELEGRAM_TEXT_LIMIT, Template, clip_text
from ..services import Services
from ..store import template_of

logger = logging.getLogger(__name__)

WAIT_SOURCE, WAIT_DESCRIBE, CONFIRM_TEMPLATE, WAIT_ADJUST, WAIT_DEST = range(5)

K_DESC = "pending_desc"
K_TEMPLATE = "pending_template"
K_EDIT_SUB = "edit_sub_id"   # edit flow: subscription id being edited
K_SRC_CTX = "src_ctx"        # edit flow: subscription id receiving new sources

MAX_SUBS_PER_USER = 20  # per-user subscription cap (anti-abuse quota)


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def _lang(svc: Services, user_id: int) -> str:
    """Resolve the UI language for a user: personal choice → instance default."""
    return svc.store.language_for(user_id, svc.settings.default_lang)


def _require_private(update: Update) -> bool:
    """Private-chat-only guard: /list and /test expose user-private data."""
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


def _blocked(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Whether the user has been suspended by the admin (see /admin block)."""
    user = _svc(context).store.get_user(user_id) or {}
    return user.get("status") == "blocked"


def _record_llm(svc: Services, user_id: int, usage: dict, detail: str) -> None:
    """Record one template compilation: qty = API calls, plus real token usage."""
    svc.store.record_usage(
        user_id, "llm", int(usage.get("calls") or 1), detail=detail,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0))


def resolve_dest_chat(store, user_id: int, chat_id: int) -> dict | None:
    """Target-channel ownership check: only the user who added the bot may pick it."""
    chat = store.get_chat(chat_id)
    if chat is None or chat.get("added_by") != user_id:
        return None
    return chat


def _owned_sub(svc: Services, user_id: int, raw_id) -> dict | None:
    """Fetch a subscription by id and re-check ownership (multi-user isolation)."""
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


def _dest_entries(lang: str, sub: dict) -> list[tuple[str, str]]:
    return [(msg.dest_label(lang, item), str(item["id"])) for item in sub["dests"]]


def _dest_text_new(lang: str, dests: list[dict]) -> str:
    return msg.dest_manager_text(lang, [msg.dest_label(lang, d) for d in dests],
                                 next_step=True)


def _dest_kb_new(svc: Services, user_id: int, lang: str, dests: list[dict]):
    entries = [(msg.dest_label(lang, d), str(i)) for i, d in enumerate(dests)]
    selected = {d["chat_id"] for d in dests}
    chats = [c for c in svc.store.list_chats(added_by=user_id) if c["chat_id"] not in selected]
    return dest_manager_kb(lang, "new", entries, chats)


def _dest_text_edit(lang: str, sub: dict) -> str:
    return msg.dest_manager_text(lang, [msg.dest_label(lang, d) for d in sub["dests"]],
                                 next_step=False)


def _dest_kb_edit(svc: Services, user_id: int, lang: str, sub: dict):
    entries = _dest_entries(lang, sub)
    selected = {d["chat_id"] for d in sub["dests"]}
    chats = [c for c in svc.store.list_chats(added_by=user_id) if c["chat_id"] not in selected]
    return dest_manager_kb(lang, str(sub["id"]), entries, chats)


async def _edit(query, text: str, reply_markup=None) -> None:
    """Edit a message; Telegram raises 'not modified' when content is identical — ignore it."""
    text = clip_text(text, TELEGRAM_TEXT_LIMIT)  # hard cap: Telegram rejects longer texts
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _channel_dest_ok(svc: Services, context: ContextTypes.DEFAULT_TYPE,
                           user_id: int, chat_id: int, lang: str) -> tuple[bool, str]:
    """Channel target: ownership + bot-is-admin + user-is-admin, triple check."""
    title = (svc.store.get_chat(chat_id) or {}).get("title") or str(chat_id)
    if resolve_dest_chat(svc.store, user_id, chat_id) is None:
        return False, msg.dest_channel_not_yours(lang, title)
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        bot_member = None
    if bot_member is None or bot_member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        return False, msg.dest_channel_invalid(lang, title)
    try:
        user_member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        user_member = None
    if user_member is None or user_member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        return False, msg.dest_user_not_admin(lang, title)
    return True, title


# ----------------------------------------------------------- fallback & logs
async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unmatched text / unknown commands in private chats: fixed hint, never silence."""
    if update.effective_message:
        svc = _svc(context)
        uid = update.effective_user.id if update.effective_user else 0
        await update.effective_message.reply_text(
            msg.fallback(_lang(svc, uid)))


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log one line per update (separate group): for 'did the message arrive' debugging."""
    user = update.effective_user
    logger.info("update #%s from %s", update.update_id, user.id if user else "-")


# -------------------------------------------------------------- basic commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    svc = _svc(context)
    lang = _lang(svc, user.id)
    if _blocked(context, user.id):
        await update.effective_message.reply_text(msg.blocked(lang))
        return
    svc.store.add_user(user.id, user.username or "", svc.settings.default_user_status)
    await update.effective_message.reply_text(msg.welcome(lang),
                                              reply_markup=main_menu_kb(lang))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    uid = update.effective_user.id if update.effective_user else 0
    await update.effective_message.reply_text(msg.help_text(_lang(svc, uid)))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    svc = _svc(context)
    uid = update.effective_user.id if update.effective_user else 0
    context.user_data.clear()
    await update.effective_message.reply_text(msg.canceled(_lang(svc, uid)))
    return ConversationHandler.END


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lang — pick the UI language (per user)."""
    svc = _svc(context)
    uid = update.effective_user.id if update.effective_user else 0
    lang = _lang(svc, uid)
    if not _require_private(update):
        await update.effective_message.reply_text(msg.private_only(lang, "/lang"))
        return
    await update.effective_message.reply_text(
        msg.lang_prompt(lang, LANG_LABELS[lang]), reply_markup=lang_kb())


async def on_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Language picker callback: persist the choice and confirm in the new language."""
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    uid = update.effective_user.id
    lang = i18n.resolve(query.data.split(":")[-1])
    svc.store.set_user_lang(uid, lang)
    try:
        await query.edit_message_text(msg.lang_set(lang))
    except TelegramError:
        pass


# ----------------------------------------------------------------- /new wizard
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    svc = _svc(context)
    lang = _lang(svc, update.effective_user.id)
    if not svc.settings.typesafe_api_key:
        await update.effective_message.reply_text(msg.unconfigured(lang))
        return ConversationHandler.END
    chat = update.effective_chat
    if chat is not None and chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(msg.new_private_only(lang))
        return ConversationHandler.END
    if _blocked(context, update.effective_user.id):
        await update.effective_message.reply_text(msg.blocked(lang))
        return ConversationHandler.END
    svc.store.add_user(update.effective_user.id, update.effective_user.username or "",
                       svc.settings.default_user_status)
    context.user_data.clear()
    await update.effective_message.reply_text(msg.ask_source(lang))
    return WAIT_SOURCE


async def on_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Wizard/edit: a channel reference arrived → validate and register it."""
    raw = update.effective_message.text or ""
    svc = _svc(context)
    lang = _lang(svc, update.effective_user.id)
    try:
        channel = normalize_channel_ref(raw)
    except ValueError as exc:
        await update.effective_message.reply_text(msg.bad_source(lang, exc))
        return WAIT_SOURCE
    try:
        info = await svc.fetcher.head(channel)
    except ChannelError as exc:
        await update.effective_message.reply_text(msg.source_unreachable(lang, exc))
        return WAIT_SOURCE

    ctx = context.user_data.get(K_SRC_CTX)
    if ctx:  # edit mode: append a source channel to an existing subscription
        sub = _owned_sub(svc, update.effective_user.id, ctx)
        if sub is None:
            await update.effective_message.reply_text(
                msg.test_sub_not_found(lang, ctx))
            return ConversationHandler.END
        if any(item["source"] == channel for item in sub["sources"]):
            await update.effective_message.reply_text(msg.source_duplicate(lang))
        else:
            svc.store.add_sub_source(sub["id"], channel, info.head_id)
            sub = svc.store.get_subscription(sub["id"]) or sub
        await update.effective_message.reply_text(
            msg.src_manager_text(lang, [item["source"] for item in sub["sources"]],
                                 next_step=False),
            reply_markup=src_manager_kb(lang, str(sub["id"]), _src_entries(sub)))
        return WAIT_SOURCE

    sources = context.user_data.setdefault("sources", [])
    if any(item["source"] == channel for item in sources):
        await update.effective_message.reply_text(msg.source_duplicate(lang))
    else:
        sources.append({"source": channel, "head_id": info.head_id})
    entries = [(item["source"], str(i)) for i, item in enumerate(sources)]
    await update.effective_message.reply_text(
        msg.src_manager_text(lang, [item["source"] for item in sources], next_step=True),
        reply_markup=src_manager_kb(lang, "new", entries))
    return WAIT_SOURCE


async def on_src_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Source manager (ms:menu / ms:rm / ms:done; ctx = "new" or a subscription id)."""
    query = update.callback_query
    svc = _svc(context)
    user_id = update.effective_user.id
    lang = _lang(svc, user_id)
    _, op, ctx, *rest = query.data.split(":")

    if ctx == "new":
        sources = context.user_data.setdefault("sources", [])
        if op == "rm":
            if len(sources) <= 1:
                await query.answer(msg.need_one_source(lang), show_alert=True)
                return WAIT_SOURCE
            sources.pop(int(rest[0]))
        elif op == "done":
            if not sources:
                await query.answer(msg.need_one_source(lang), show_alert=True)
                return WAIT_SOURCE
            await query.answer()
            await _edit(query, msg.ask_describe(lang))
            return WAIT_DESCRIBE
        await query.answer()
        entries = [(item["source"], str(i)) for i, item in enumerate(sources)]
        await _edit(query,
                    msg.src_manager_text(lang, [item["source"] for item in sources],
                                         next_step=True),
                    reply_markup=src_manager_kb(lang, "new", entries))
        return WAIT_SOURCE

    in_conv = context.user_data.get(K_SRC_CTX) == ctx
    sub = _owned_sub(svc, user_id, ctx)
    if sub is None:
        await query.answer()
        await _edit(query, msg.test_sub_not_found(lang, ctx))
        return ConversationHandler.END
    if op == "rm":
        if len(sub["sources"]) <= 1:
            await query.answer(msg.need_one_source(lang), show_alert=True)
            return WAIT_SOURCE if in_conv else ConversationHandler.END
        svc.store.remove_sub_source(int(rest[0]))
        sub = svc.store.get_subscription(sub["id"]) or sub
    elif op == "done":
        await query.answer()
        context.user_data.pop(K_SRC_CTX, None)
        await _edit(query, msg.edit_menu_text(lang, sub, template_of(sub)),
                    reply_markup=edit_menu_kb(lang, sub["id"]))
        return ConversationHandler.END
    await query.answer()
    await _edit(query,
                msg.src_manager_text(lang, [item["source"] for item in sub["sources"]],
                                     next_step=False),
                reply_markup=src_manager_kb(lang, str(sub["id"]), _src_entries(sub)))
    return WAIT_SOURCE if in_conv else ConversationHandler.END


async def on_src_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Edit a subscription: open the source manager (send names/links to add).

    Conversation entry point (ms:add:<id> / ms:menu:<id>): without conversation
    state, typed channel links fall through to the plain-text fallback instead of
    being registered."""
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    lang = _lang(svc, update.effective_user.id)
    _, _, raw_id = query.data.split(":")
    sub = _owned_sub(svc, update.effective_user.id, raw_id)
    if sub is None:
        await _edit(query, msg.test_sub_not_found(lang, raw_id))
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data[K_SRC_CTX] = str(sub["id"])
    await _edit(query,
                msg.src_manager_text(lang, [item["source"] for item in sub["sources"]],
                                     next_step=False),
                reply_markup=src_manager_kb(lang, str(sub["id"]), _src_entries(sub)))
    return WAIT_SOURCE


async def on_describe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    svc = _svc(context)
    lang = _lang(svc, update.effective_user.id)
    if text.startswith("{"):  # power mode: paste a template JSON directly
        try:
            template = Template.model_validate_json(text)
        except ValueError as exc:
            await update.effective_message.reply_text(
                msg.bad_template_json(lang, str(exc)[:300]))
            return WAIT_DESCRIBE
    else:
        if svc.compiler is None:
            await update.effective_message.reply_text(msg.llm_not_configured(lang))
            return WAIT_DESCRIBE
        await update.effective_message.reply_text(msg.compiling(lang))
        try:
            template, llm_usage = await svc.compiler.compile(text)
        except LLMError as exc:
            _record_llm(svc, update.effective_user.id,
                        getattr(exc, "usage", {}) or {}, "failed")
            await update.effective_message.reply_text(
                msg.compile_failed(lang, str(exc)[:300]))
            return WAIT_DESCRIBE
        _record_llm(svc, update.effective_user.id, llm_usage, text[:60])
    context.user_data[K_DESC] = text
    context.user_data[K_TEMPLATE] = template.model_dump()
    confirm = msg.template_confirm(lang, template_summary(lang, template),
                                   edit=K_EDIT_SUB in context.user_data)
    await update.effective_message.reply_text(confirm, reply_markup=template_kb(lang))
    return CONFIRM_TEMPLATE


async def on_template_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    lang = _lang(svc, update.effective_user.id)
    action = query.data.split(":", 1)[1]
    editing = context.user_data.get(K_EDIT_SUB)
    if action == "redo":
        await _edit(query, msg.edit_template_ask(lang) if editing else msg.ask_describe(lang))
        return WAIT_DESCRIBE
    if action == "adjust":
        await _edit(query, msg.ask_adjust(lang))
        return WAIT_ADJUST
    template = _pending_template(context)
    if editing:  # edit mode: confirming overwrites the stored template
        svc.store.set_subscription(int(editing), template_json=template.model_dump_json())
        context.user_data.clear()
        sub = svc.store.get_subscription(int(editing))
        if sub is not None:
            await _edit(query, f"{msg.edit_done(lang)}\n\n{msg.sub_line(lang, sub, template)}",
                        reply_markup=sub_actions_kb(lang, sub))
        else:
            await _edit(query, msg.edit_done(lang))
        return ConversationHandler.END
    context.user_data.setdefault("dests", [])
    dests = context.user_data["dests"]
    await _edit(query, _dest_text_new(lang, dests),
                reply_markup=_dest_kb_new(svc, update.effective_user.id, lang, dests))
    return WAIT_DEST


async def on_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    feedback = (update.effective_message.text or "").strip()
    svc = _svc(context)
    lang = _lang(svc, update.effective_user.id)
    if svc.compiler is None:
        await update.effective_message.reply_text(msg.llm_not_configured(lang))
        return WAIT_ADJUST
    await update.effective_message.reply_text(msg.compiling(lang))
    try:
        template, llm_usage = await svc.compiler.compile(
            context.user_data.get(K_DESC, ""),
            feedback=feedback,
            previous=_pending_template(context))
    except LLMError as exc:
        _record_llm(svc, update.effective_user.id,
                    getattr(exc, "usage", {}) or {}, "failed")
        await update.effective_message.reply_text(msg.compile_failed(lang, str(exc)[:300]))
        return WAIT_ADJUST
    _record_llm(svc, update.effective_user.id, llm_usage, feedback[:60])
    context.user_data[K_TEMPLATE] = template.model_dump()
    confirm = msg.template_confirm(lang, template_summary(lang, template),
                                   edit=K_EDIT_SUB in context.user_data)
    await update.effective_message.reply_text(confirm, reply_markup=template_kb(lang))
    return CONFIRM_TEMPLATE


async def on_dest_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Target manager (md:menu / md:rm / md:dm / md:ch / md:refresh / md:done)."""
    query = update.callback_query
    svc = _svc(context)
    user = update.effective_user
    lang = _lang(svc, user.id)
    _, op, ctx, *rest = query.data.split(":")

    if ctx == "new":
        dests = context.user_data.setdefault("dests", [])
        if op == "rm":
            if len(dests) <= 1:
                await query.answer(msg.need_one_dest(lang), show_alert=True)
                return WAIT_DEST
            dests.pop(int(rest[0]))
        elif op == "dm":
            if any(d["chat_id"] == user.id for d in dests):
                await query.answer(msg.dest_duplicate(lang), show_alert=True)
                return WAIT_DEST
            dests.append({"kind": "dm", "chat_id": user.id, "title": "DM"})
        elif op == "ch":
            chat_id = int(rest[0])
            if any(d["chat_id"] == chat_id for d in dests):
                await query.answer(msg.dest_duplicate(lang), show_alert=True)
                return WAIT_DEST
            ok, title = await _channel_dest_ok(svc, context, user.id, chat_id, lang)
            if not ok:
                await query.answer()
                await _edit(query, f"{title}\n\n{_dest_text_new(lang, dests)}",
                            reply_markup=_dest_kb_new(svc, user.id, lang, dests))
                return WAIT_DEST
            dests.append({"kind": "channel", "chat_id": chat_id, "title": title})
        elif op == "done":
            if not dests:
                await query.answer(msg.need_one_dest(lang), show_alert=True)
                return WAIT_DEST
            await query.answer()
            outcome = await _finish_new(query, context, svc, user, lang)
            context.user_data.clear()
            return outcome
        await query.answer(msg.toast_refreshed(lang) if op == "refresh" else None)
        await _edit(query, _dest_text_new(lang, dests),
                    reply_markup=_dest_kb_new(svc, user.id, lang, dests))
        return WAIT_DEST

    sub = _owned_sub(svc, user.id, ctx)
    if sub is None:
        await query.answer()
        await _edit(query, msg.test_sub_not_found(lang, ctx))
        return ConversationHandler.END
    if op == "rm":
        if len(sub["dests"]) <= 1:
            await query.answer(msg.need_one_dest(lang), show_alert=True)
            return ConversationHandler.END
        svc.store.remove_sub_dest(int(rest[0]))
    elif op == "dm":
        if any(d["chat_id"] == user.id for d in sub["dests"]):
            await query.answer(msg.dest_duplicate(lang), show_alert=True)
            return ConversationHandler.END
        svc.store.add_sub_dest(sub["id"], "dm", user.id, "DM")
    elif op == "ch":
        chat_id = int(rest[0])
        if any(d["chat_id"] == chat_id for d in sub["dests"]):
            await query.answer(msg.dest_duplicate(lang), show_alert=True)
            return ConversationHandler.END
        ok, title = await _channel_dest_ok(svc, context, user.id, chat_id, lang)
        if not ok:
            await query.answer()
            await _edit(query, f"{title}\n\n{_dest_text_edit(lang, sub)}",
                        reply_markup=_dest_kb_edit(svc, user.id, lang, sub))
            return ConversationHandler.END
        svc.store.add_sub_dest(sub["id"], "channel", chat_id, title)
    elif op == "done":
        await query.answer()
        await _edit(query, msg.edit_menu_text(lang, sub, template_of(sub)),
                    reply_markup=edit_menu_kb(lang, sub["id"]))
        return ConversationHandler.END
    await query.answer(msg.toast_refreshed(lang) if op == "refresh" else None)
    sub = svc.store.get_subscription(sub["id"]) or sub
    await _edit(query, _dest_text_edit(lang, sub),
                reply_markup=_dest_kb_edit(svc, user.id, lang, sub))
    return ConversationHandler.END


async def _finish_new(query, context: ContextTypes.DEFAULT_TYPE, svc: Services,
                      user, lang: str) -> int:
    """Wizard finale: sources + targets complete → create the subscription.

    Refresh cadence is one global schedule owned by the admin (/admin interval),
    so no interval is asked here.
    """
    cap = (svc.store.get_user(user.id) or {}).get("max_subs") or MAX_SUBS_PER_USER
    if len(svc.store.list_subscriptions(user_id=user.id)) >= cap:
        await _edit(query, msg.subs_limit(lang, cap))
        return ConversationHandler.END
    sources = context.user_data.get("sources") or []
    dests = context.user_data.get("dests") or []
    if not sources:
        await _edit(query, msg.need_one_source(lang))
        return ConversationHandler.END
    template = _pending_template(context)
    sub_id = svc.store.add_subscription(
        user_id=user.id, template=template,
        sources=[{"source": item["source"], "last_seen_id": item.get("head_id")}
                 for item in sources],
        dests=dests)
    sep = i18n.t(lang, "list_sep")
    src_names = sep.join(f"@{item['source']}" for item in sources)
    dest_names = sep.join(msg.dest_label(lang, d) for d in dests)
    await _edit(query, msg.sub_created(lang, sub_id, src_names,
                                       match_summary(lang, template), dest_names))
    return ConversationHandler.END


# ------------------------------------------------------------ subscription mgmt
def _list_view(lang: str, subs: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Item picker view: overview text + one button row per subscription."""
    lines = [msg.list_head(lang, len(subs))]
    lines += [msg.sub_pick_line(lang, i, sub) for i, sub in enumerate(subs, 1)]
    lines += ["", msg.list_hint(lang)]
    return "\n".join(lines), sub_pick_kb(lang, subs)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    uid = update.effective_user.id if update.effective_user else 0
    lang = _lang(svc, uid)
    if not _require_private(update):
        await update.effective_message.reply_text(msg.private_only(lang, "/list"))
        return
    if _blocked(context, uid):
        await update.effective_message.reply_text(msg.blocked(lang))
        return
    subs = svc.store.list_subscriptions(user_id=uid)
    if not subs:
        await update.effective_message.reply_text(msg.no_subs(lang))
        return
    text, kb = _list_view(lang, subs)
    await update.effective_message.reply_text(text, reply_markup=kb)


async def on_sub_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    user_id = update.effective_user.id
    lang = _lang(svc, user_id)
    parts = query.data.split(":")
    if len(parts) == 2 and parts[1] == "list":  # "back to list": the picker view
        subs = svc.store.list_subscriptions(user_id=user_id)
        if not subs:
            await _edit(query, msg.no_subs(lang))
            return
        text, kb = _list_view(lang, subs)
        await _edit(query, text, reply_markup=kb)
        return
    _, action, raw_id = parts
    sub_id = int(raw_id)
    sub = svc.store.get_subscription(sub_id)
    if sub is None or sub["user_id"] != user_id:
        await _edit(query, msg.test_sub_not_found(lang, sub_id))
        return
    if action in ("pause", "resume"):
        svc.store.set_subscription(sub_id, enabled=1 if action == "resume" else 0)
        sub = svc.store.get_subscription(sub_id) or sub
        await _edit(query, msg.sub_line(lang, sub, template_of(sub)),
                    reply_markup=sub_actions_kb(lang, sub))
    elif action == "delete":
        svc.store.delete_subscription(sub_id)
        subs = svc.store.list_subscriptions(user_id=user_id)
        if subs:
            text, kb = _list_view(lang, subs)
            await _edit(query, f"{msg.sub_deleted(lang, sub_id)}\n\n{text}",
                        reply_markup=kb)
        else:
            await _edit(query, f"{msg.sub_deleted(lang, sub_id)}\n\n{msg.no_subs(lang)}")
    elif action == "test":
        await _edit(query, msg.testing(lang))
        result = await svc.pipeline.preview(sub, pool=100, limit=6)
        await _edit(query, msg.test_result(lang, result, sub, template_of(sub)),
                    reply_markup=back_list_kb(lang))
    elif action == "edit":
        await _edit(query, msg.edit_menu_text(lang, sub, template_of(sub)),
                    reply_markup=edit_menu_kb(lang, sub_id))
    elif action in ("show", "open"):  # open an item: detail + action buttons
        await _edit(query, msg.sub_line(lang, sub, template_of(sub)),
                    reply_markup=sub_actions_kb(lang, sub))


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    user_id = update.effective_user.id if update.effective_user else 0
    lang = _lang(svc, user_id)
    if not _require_private(update):
        await update.effective_message.reply_text(msg.private_only(lang, "/test"))
        return
    if _blocked(context, user_id):
        await update.effective_message.reply_text(msg.blocked(lang))
        return
    args = context.args or []
    subs = svc.store.list_subscriptions(user_id=user_id)
    if not subs:
        await update.effective_message.reply_text(msg.no_subs(lang))
        return
    sub = None
    if args and args[0].isdigit():
        sub = svc.store.get_subscription(int(args[0]))
        if sub is not None and sub["user_id"] != user_id:
            sub = None
        if sub is None:
            await update.effective_message.reply_text(
                msg.test_sub_not_found(lang, args[0]))
            return
    elif len(subs) == 1:
        sub = subs[0]
    else:
        await update.effective_message.reply_text(msg.test_need_id(lang))
        return
    placeholder = await update.effective_message.reply_text(msg.testing(lang))
    result = await svc.pipeline.preview(sub, pool=100, limit=6)
    await placeholder.edit_text(msg.test_result(lang, result, sub, template_of(sub)))


# ---------------------------------------------------------- subscription editing
async def on_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Edit a subscription: enter the "re-describe the filter" flow (overwrites on confirm)."""
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    lang = _lang(svc, update.effective_user.id)
    _, raw_id = query.data.split(":")
    sub = _owned_sub(svc, update.effective_user.id, raw_id)
    if sub is None:
        await _edit(query, msg.test_sub_not_found(lang, raw_id))
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data[K_EDIT_SUB] = str(sub["id"])
    context.user_data[K_DESC] = template_of(sub).name or ""
    await _edit(query, msg.edit_template_ask(lang))
    return WAIT_DESCRIBE


async def on_ui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    svc = _svc(context)
    uid = update.effective_user.id if update.effective_user else 0
    lang = _lang(svc, uid)
    if query.data == "ui:list":
        await cmd_list(update, context)
    elif query.data == "ui:help":
        await update.effective_message.reply_text(msg.help_text(lang))


# --------------------------------------------------------------- channel events
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if change is None:
        return
    chat = change.chat
    if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    status = change.new_chat_member.status
    svc = _svc(context)
    store = svc.store
    adminish = status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    can_post = adminish if chat.type == ChatType.CHANNEL else (
        adminish or status == ChatMember.MEMBER)
    if can_post:
        adder = change.from_user.id if change.from_user else None
        store.upsert_chat(chat.id, chat.type, chat.title or "", adder)
        if adder:
            lang = _lang(svc, adder)
            try:
                await context.bot.send_message(adder, msg.chat_added(lang, chat.title or str(chat.id)))
            except TelegramError:
                pass
    elif status in (ChatMember.LEFT, ChatMember.BANNED):
        store.remove_chat(chat.id)
    elif chat.type == ChatType.CHANNEL and status == ChatMember.MEMBER:
        # demoted inside a channel (admin → member): cannot post anymore, drop it
        store.remove_chat(chat.id)


# ------------------------------------------------------------------- fallbacks
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("handler error", exc_info=context.error)
    message = update.effective_message if isinstance(update, Update) else None
    if message is not None:
        try:
            svc = context.application.bot_data.get("services")
            uid = update.effective_user.id if update.effective_user else 0
            lang = _lang(svc, uid) if svc else i18n.resolve(None)
            await message.reply_text(msg.error(lang, str(context.error)[:200]))
        except TelegramError:
            pass
