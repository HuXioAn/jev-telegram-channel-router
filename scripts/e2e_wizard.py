#!/usr/bin/env python3
"""端到端向导调试：用合成 Update 驱动真实 Application。

真实链路：真 Bot API 会话（消息真的发到用户 DM）/ 真 DeepSeek 编译 / 真 Jev 试跑。
前提：目标用户已在该 bot 私聊里按过 /start（否则 bot 不能主动发消息）。

用法：
    python scripts/e2e_wizard.py <user_id> [channel] [描述]

效果：用户 DM 会依次收到：/start 欢迎 → /new 提问 → 频道确认 → 模板确认（带按钮）
     → 目的地选择 → 频率选择 → 订阅创建 → /test 试跑结果。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User  # noqa: E402

from tgfilter.bot.app import build_application  # noqa: E402
from tgfilter.config import Settings  # noqa: E402

_state = {"update_id": 1000, "sent": []}


def _next_id() -> int:
    _state["update_id"] += 1
    return _state["update_id"]


def _user(user_id: int) -> User:
    return User(id=user_id, first_name="Anton", is_bot=False)


def _message(user_id: int, text: str, entities=None) -> Message:
    return Message(message_id=_next_id(), date=datetime.now(timezone.utc),
                   chat=Chat(id=user_id, type="private"), from_user=_user(user_id),
                   text=text, entities=entities or [])


def _command(user_id: int, text: str) -> Message:
    command = text.split()[0]
    entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(command))
    return _message(user_id, text, [entity])


def _callback(user_id: int, message: Message, data: str) -> Update:
    return Update(update_id=_next_id(), callback_query=CallbackQuery(
        id=str(_next_id()), from_user=_user(user_id), chat_instance="e2e-debug",
        message=message, data=data))


async def _step(app, label: str, update: Update) -> None:
    print(f"[步骤] {label}")
    await app.process_update(update)


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：python scripts/e2e_wizard.py <user_id> [channel] [描述]")
    user_id = int(sys.argv[1])
    channel = sys.argv[2] if len(sys.argv) > 2 else "Financial_Express"
    description = sys.argv[3] if len(sys.argv) > 3 else "中国相关的重磅财经消息，重要度高的"

    settings = Settings.load()
    app = build_application(settings)

    original_send = app.bot.send_message
    original_edit = app.bot.edit_message_text

    async def send_message(*args, **kwargs):
        message = await original_send(*args, **kwargs)
        if isinstance(message, Message):
            _state["sent"].append(message)
            print(f"   → 发送 #{message.message_id}: {message.text[:70]!r}")
        return message

    async def edit_message_text(*args, **kwargs):
        result = await original_edit(*args, **kwargs)
        if isinstance(result, Message):
            print(f"   → 编辑 #{result.message_id}: {result.text[:70]!r}")
        return result

    app.bot.send_message = send_message
    app.bot.edit_message_text = edit_message_text

    original_answer = CallbackQuery.answer

    async def noop_answer(self, *args, **kwargs):  # 合成 callback 的 id 无效，屏蔽 answer
        return True

    CallbackQuery.answer = noop_answer
    await app.initialize()
    try:
        await _step(app, "/start", Update(update_id=_next_id(),
                                          message=_command(user_id, "/start")))
        await _step(app, "/new", Update(update_id=_next_id(),
                                        message=_command(user_id, "/new")))
        await _step(app, f"发送频道 {channel}",
                    Update(update_id=_next_id(), message=_message(user_id, channel)))
        await _step(app, f"发送描述「{description}」",
                    Update(update_id=_next_id(), message=_message(user_id, description)))

        confirm = next((m for m in reversed(_state["sent"])
                        if m.text and m.text.startswith("📋")), None)
        if confirm is None:
            print("❌ 未找到模板确认消息，终止")
            return
        await _step(app, "点击「✅ 使用这个模板」", _callback(user_id, confirm, "tpl:confirm"))
        await _step(app, "点击「📬 发到我的私聊」", _callback(user_id, confirm, "dst:dm"))
        await _step(app, "点击「20 分钟」", _callback(user_id, confirm, "iv:20"))

        sub_id = None
        for message in reversed(_state["sent"]):
            if message.text and message.text.startswith("✅ 订阅 #"):
                sub_id = int(message.text.split("#", 2)[1].split(" ", 1)[0])
                break
        if sub_id is None:
            print("❌ 未找到「订阅已创建」消息，终止")
            return

        await _step(app, f"/test {sub_id}",
                    Update(update_id=_next_id(),
                           message=_command(user_id, f"/test {sub_id}")))

        from tgfilter.store import Store
        store = Store(settings.db_path)
        row = store.get_subscription(sub_id)
        print("\n[数据库] 订阅行：")
        for key in ("id", "user_id", "source", "dest_kind", "dest_chat_id",
                    "dest_title", "interval_minutes", "enabled", "last_seen_id"):
            print(f"   {key} = {row[key]}")
    finally:
        CallbackQuery.answer = original_answer
        await app.shutdown()
    print("\n✅ 端到端向导调试完成")


if __name__ == "__main__":
    asyncio.run(main())
