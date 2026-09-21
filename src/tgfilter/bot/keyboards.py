"""Inline keyboard builders (button labels localized via tgfilter.i18n)."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import LANG_LABELS, t
from .messages import dest_label


def main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_new"), callback_data="ui:new"),
         InlineKeyboardButton(t(lang, "btn_list"), callback_data="ui:list")],
        [InlineKeyboardButton(t(lang, "btn_help"), callback_data="ui:help")],
    ])


def template_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_tpl_use"), callback_data="tpl:confirm"),
         InlineKeyboardButton(t(lang, "btn_tpl_redo"), callback_data="tpl:redo")],
        [InlineKeyboardButton(t(lang, "btn_tpl_adjust"), callback_data="tpl:adjust")],
    ])


def src_manager_kb(lang: str, ctx: str, sources: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Source manager; sources = [(channel, callback key)], ctx = "new" or a sub id."""
    rows = [[InlineKeyboardButton(t(lang, "btn_rm", name=f"@{name}"),
                                  callback_data=f"ms:rm:{ctx}:{key}")]
            for name, key in sources]
    rows.append([InlineKeyboardButton(t(lang, "btn_done"), callback_data=f"ms:done:{ctx}")])
    return InlineKeyboardMarkup(rows)


def dest_manager_kb(lang: str, ctx: str, dests: list[tuple[str, str]],
                    chats: list[dict]) -> InlineKeyboardMarkup:
    """Target manager; dests = [(label, callback key)], chats = addable channels."""
    rows = [[InlineKeyboardButton(t(lang, "btn_rm", name=name),
                                  callback_data=f"md:rm:{ctx}:{key}")]
            for name, key in dests]
    rows.append([InlineKeyboardButton(t(lang, "btn_add_dm"), callback_data=f"md:dm:{ctx}")])
    for chat in chats[:8]:
        title = (chat["title"] or str(chat["chat_id"]))[:40]
        rows.append([InlineKeyboardButton(t(lang, "btn_add_channel", title=title),
                                          callback_data=f"md:ch:{ctx}:{chat['chat_id']}")])
    rows.append([InlineKeyboardButton(t(lang, "btn_refresh"), callback_data=f"md:refresh:{ctx}"),
                 InlineKeyboardButton(t(lang, "btn_done"), callback_data=f"md:done:{ctx}")])
    return InlineKeyboardMarkup(rows)


def sub_actions_kb(lang: str, sub: dict) -> InlineKeyboardMarkup:
    toggle_text, toggle_action = ((t(lang, "btn_pause"), "pause") if sub["enabled"]
                                  else (t(lang, "btn_resume"), "resume"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=f"sub:{toggle_action}:{sub['id']}"),
         InlineKeyboardButton(t(lang, "btn_test"), callback_data=f"sub:test:{sub['id']}")],
        [InlineKeyboardButton(t(lang, "btn_edit"), callback_data=f"sub:edit:{sub['id']}"),
         InlineKeyboardButton(t(lang, "btn_delete"), callback_data=f"sub:delete:{sub['id']}")],
        [InlineKeyboardButton(t(lang, "btn_back_list"), callback_data="sub:list")],
    ])


def sub_pick_kb(lang: str, subs: list[dict]) -> InlineKeyboardMarkup:
    """Item picker: one row per subscription; opening one shows its action buttons."""
    rows = []
    for index, sub in enumerate(subs, 1):
        first = sub["dests"][0] if sub["dests"] else None
        dest = dest_label(lang, first) if first else "?"
        extra = t(lang, "pick_more", n=len(sub["dests"]) - 1) if len(sub["dests"]) > 1 else ""
        state = "" if sub["enabled"] else " ⏸"
        label = f"{index}. #{sub['id']} · {dest}{extra}{state}"
        rows.append([InlineKeyboardButton(label[:60],
                                          callback_data=f"sub:open:{sub['id']}")])
    return InlineKeyboardMarkup(rows)


def back_list_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "btn_back_list"), callback_data="sub:list")]])


def edit_menu_kb(lang: str, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_sources"), callback_data=f"ms:menu:{sub_id}"),
         InlineKeyboardButton(t(lang, "btn_dests"), callback_data=f"md:menu:{sub_id}")],
        [InlineKeyboardButton(t(lang, "btn_template"), callback_data=f"etpl:{sub_id}")],
        [InlineKeyboardButton(t(lang, "btn_back"), callback_data=f"sub:show:{sub_id}")],
    ])


def lang_kb() -> InlineKeyboardMarkup:
    """Language picker for /lang (labels are language-native, not translated)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(LANG_LABELS["en"], callback_data="lang:set:en"),
         InlineKeyboardButton(LANG_LABELS["zh"], callback_data="lang:set:zh")],
    ])
