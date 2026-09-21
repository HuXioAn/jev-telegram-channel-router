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


def src_manager_kb(ctx: str, sources: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """源频道管理器；sources = [(频道名, 回调 key)]，ctx = "new" 或订阅编号。"""
    rows = [[InlineKeyboardButton(f"❌ @{name}", callback_data=f"ms:rm:{ctx}:{key}")]
            for name, key in sources]
    rows.append([InlineKeyboardButton("✅ 完成", callback_data=f"ms:done:{ctx}")])
    return InlineKeyboardMarkup(rows)


def dest_manager_kb(ctx: str, dests: list[tuple[str, str]],
                    chats: list[dict]) -> InlineKeyboardMarkup:
    """目的地管理器；dests = [(显示名, 回调 key)]，chats = 可添加的频道。"""
    rows = [[InlineKeyboardButton(f"❌ {name}", callback_data=f"md:rm:{ctx}:{key}")]
            for name, key in dests]
    rows.append([InlineKeyboardButton("📬 加私聊", callback_data=f"md:dm:{ctx}")])
    for chat in chats[:8]:
        title = (chat["title"] or str(chat["chat_id"]))[:40]
        rows.append([InlineKeyboardButton(f"📢 加 {title}",
                                          callback_data=f"md:ch:{ctx}:{chat['chat_id']}")])
    rows.append([InlineKeyboardButton("🔄 刷新频道列表", callback_data=f"md:refresh:{ctx}"),
                 InlineKeyboardButton("✅ 完成", callback_data=f"md:done:{ctx}")])
    return InlineKeyboardMarkup(rows)


def interval_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{m} 分钟", callback_data=f"iv:{m}") for m in (10, 20)],
        [InlineKeyboardButton(f"{m} 分钟", callback_data=f"iv:{m}") for m in (30, 60)],
    ])


def edit_interval_kb(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{m} 分钟", callback_data=f"eivs:{sub_id}:{m}")
         for m in (10, 20)],
        [InlineKeyboardButton(f"{m} 分钟", callback_data=f"eivs:{sub_id}:{m}")
         for m in (30, 60)],
    ])


def sub_actions_kb(sub: dict) -> InlineKeyboardMarkup:
    toggle_text, toggle_action = ("⏸ 暂停", "pause") if sub["enabled"] else ("▶️ 恢复", "resume")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=f"sub:{toggle_action}:{sub['id']}"),
         InlineKeyboardButton("🧪 试跑", callback_data=f"sub:test:{sub['id']}")],
        [InlineKeyboardButton("✏️ 编辑", callback_data=f"sub:edit:{sub['id']}"),
         InlineKeyboardButton("🗑 删除", callback_data=f"sub:delete:{sub['id']}")],
        [InlineKeyboardButton("⬅️ 返回列表", callback_data="sub:list")],
    ])


def sub_pick_kb(subs: list[dict]) -> InlineKeyboardMarkup:
    """条目选择菜单：一行一个订阅；打开后进入该条目的操作按钮。"""
    rows = []
    for index, sub in enumerate(subs, 1):
        dest = next((d["title"] or str(d["chat_id"]) for d in sub["dests"]), "?")
        extra = f" 等 {len(sub['dests'])} 个" if len(sub["dests"]) > 1 else ""
        state = "" if sub["enabled"] else " ⏸"
        label = f"{index}. #{sub['id']} · {dest}{extra}{state}"
        rows.append([InlineKeyboardButton(label[:60],
                                          callback_data=f"sub:open:{sub['id']}")])
    return InlineKeyboardMarkup(rows)


def back_list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ 返回列表", callback_data="sub:list")]])


def edit_menu_kb(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 源频道", callback_data=f"ms:menu:{sub_id}"),
         InlineKeyboardButton("📬 目的地", callback_data=f"md:menu:{sub_id}")],
        [InlineKeyboardButton("⏱ 频率", callback_data=f"eiv:{sub_id}"),
         InlineKeyboardButton("🧩 筛选模板", callback_data=f"etpl:{sub_id}")],
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"sub:show:{sub_id}")],
    ])
