"""Inline 键盘构建。"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 新建订阅", callback_data="ui:new"),
         InlineKeyboardButton("📋 我的订阅", callback_data="ui:list")],
        [InlineKeyboardButton("📖 帮助", callback_data="ui:help")],
    ])


def template_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 使用这个模板", callback_data="tpl:confirm"),
         InlineKeyboardButton("✏️ 重新描述", callback_data="tpl:redo")],
        [InlineKeyboardButton("🔧 让 AI 调整", callback_data="tpl:adjust")],
    ])


def dest_kb(chats: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📬 发到我的私聊", callback_data="dst:dm")]]
    for chat in chats[:8]:
        title = (chat["title"] or str(chat["chat_id"]))[:40]
        rows.append([InlineKeyboardButton(f"📢 {title}",
                                          callback_data=f"dst:ch:{chat['chat_id']}")])
    rows.append([InlineKeyboardButton("🔄 刷新列表", callback_data="dst:refresh")])
    return InlineKeyboardMarkup(rows)


def interval_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{m} 分钟", callback_data=f"iv:{m}") for m in (10, 20)],
        [InlineKeyboardButton(f"{m} 分钟", callback_data=f"iv:{m}") for m in (30, 60)],
    ])


def sub_actions_kb(sub: dict) -> InlineKeyboardMarkup:
    toggle_text, toggle_action = ("⏸ 暂停", "pause") if sub["enabled"] else ("▶️ 恢复", "resume")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=f"sub:{toggle_action}:{sub['id']}"),
         InlineKeyboardButton("🧪 试跑", callback_data=f"sub:test:{sub['id']}")],
        [InlineKeyboardButton("🗑 删除", callback_data=f"sub:delete:{sub['id']}")],
    ])
