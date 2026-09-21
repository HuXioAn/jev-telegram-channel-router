#!/usr/bin/env python3
"""端到端调试：用合成 Update 驱动真实 Application。

真实链路：真 Bot API 会话（消息真的发到用户 DM）/ 真 DeepSeek 编译 / 真 Jev 试跑。
前提：目标用户已在该 bot 私聊里按过 /start（否则 bot 不能主动发消息）。

用法：
    python scripts/e2e_wizard.py <user_id> [channel] [描述]     # 完整向导 + 试跑
    python scripts/e2e_wizard.py <user_id> --test <订阅编号>     # 只跑 /test 试跑

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
from telegram.ext import ExtBot  # noqa: E402

from tgfilter.bot.app import build_application  # noqa: E402
from tgfilter.config import Settings  # noqa: E402
from tgfilter.store import Store  # noqa: E402

_state = {"update_id": 1000, "outbox": []}  # outbox：bot 发出的所有消息（含编辑后的）


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


def _attach(update: Update, bot) -> Update:
    """手工构造的 PTB 对象需显式绑定 bot，否则 reply_text / edit 等快捷方法不可用。"""
    update.set_bot(bot)
    if update.message is not None:
        update.message.set_bot(bot)
    if update.callback_query is not None:
        update.callback_query.set_bot(bot)
        if update.callback_query.message is not None:
            update.callback_query.message.set_bot(bot)
    return update


async def _step(app, label: str, update: Update) -> None:
    print(f"[步骤] {label}")
    await app.process_update(_attach(update, app.bot))


def _detect_sub_id() -> int | None:
    for message in reversed(_state["outbox"]):
        if message.text and message.text.startswith("✅ 订阅 #"):
            return int(message.text.split("#", 2)[1].split(" ", 1)[0])
    return None


async def run_wizard(app, user_id: int, channel: str, description: str) -> int | None:
    await _step(app, "/start", Update(update_id=_next_id(),
                                      message=_command(user_id, "/start")))
    await _step(app, "/new", Update(update_id=_next_id(),
                                    message=_command(user_id, "/new")))
    await _step(app, f"发送频道 {channel}",
                Update(update_id=_next_id(), message=_message(user_id, channel)))
    manager = next((m for m in reversed(_state["outbox"])
                    if m.text and m.text.startswith("📡")), None)
    if manager is None:
        print("❌ 未找到源频道管理器消息")
        return None
    await _step(app, "点击「✅ 完成」（源频道）",
                _callback(user_id, manager, "ms:done:new"))
    await _step(app, f"发送描述「{description}」",
                Update(update_id=_next_id(), message=_message(user_id, description)))

    confirm = next((m for m in reversed(_state["outbox"])
                    if m.text and m.text.startswith("📋")), None)
    if confirm is None:
        print("❌ 未找到模板确认消息")
        return None
    await _step(app, "点击「✅ 使用这个模板」", _callback(user_id, confirm, "tpl:confirm"))
    await _step(app, "点击「📬 加私聊」", _callback(user_id, confirm, "md:dm:new"))
    await _step(app, "点击「✅ 完成」（目的地）→ 直接创建订阅",
                _callback(user_id, confirm, "md:done:new"))
    return _detect_sub_id()


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(
            "用法：python scripts/e2e_wizard.py <user_id> [channel] [描述]\n"
            "      python scripts/e2e_wizard.py <user_id> --test <订阅编号>")
    user_id = int(args[0])
    test_mode = "--test" in args
    channel = args[1] if len(args) > 1 and args[1] != "--test" else "Financial_Express"
    description = (args[2] if len(args) > 2 and args[2] != "--test"
                   else "中国相关的重磅财经消息，重要度高的")

    settings = Settings.load()
    app = build_application(settings)

    # PTB 的 TelegramObject 禁止实例级 monkeypatch（__slots__ 保护），改在类上打补丁
    original_send = ExtBot.send_message
    original_edit = ExtBot.edit_message_text

    async def send_message(self, *args, **kwargs):
        message = await original_send(self, *args, **kwargs)
        if isinstance(message, Message):
            _state["outbox"].append(message)
            print(f"   → 发送 #{message.message_id}: {(message.text or '')[:70]!r}")
        return message

    async def edit_message_text(self, *args, **kwargs):
        result = await original_edit(self, *args, **kwargs)
        if isinstance(result, Message):
            _state["outbox"].append(result)
            print(f"   → 编辑 #{result.message_id}: {(result.text or '')[:70]!r}")
        return result

    ExtBot.send_message = send_message
    ExtBot.edit_message_text = edit_message_text

    original_answer = CallbackQuery.answer

    async def noop_answer(self, *args, **kwargs):  # 合成 callback 的 id 无效，屏蔽 answer
        return True

    CallbackQuery.answer = noop_answer
    await app.initialize()
    # initialize() 不会触发 post_init（只有 run_polling/run_webhook 会），这里手动装配 services
    if app.post_init is not None:
        await app.post_init(app)

    sub_id: int | None = None
    try:
        if test_mode:
            sub_id = int(args[args.index("--test") + 1])
            await _step(app, f"/test {sub_id}",
                        Update(update_id=_next_id(),
                               message=_command(user_id, f"/test {sub_id}")))
        else:
            sub_id = await run_wizard(app, user_id, channel, description)
            if sub_id is None:
                print("❌ 向导未能完成（未创建订阅）")
                return
            await _step(app, f"/test {sub_id}",
                        Update(update_id=_next_id(),
                               message=_command(user_id, f"/test {sub_id}")))

        store = Store(settings.db_path)
        row = store.get_subscription(sub_id)
        print("\n[数据库] 订阅：")
        print(f"   id = {row['id']}｜user_id = {row['user_id']}")
        print(f"   源频道 = {[s['source'] for s in row['sources']]}")
        print(f"   游标 = {[s['last_seen_id'] for s in row['sources']]}")
        print(f"   目的地 = {[(d['kind'], d['chat_id'], d['title']) for d in row['dests']]}")
        print(f"   enabled = {row['enabled']}（刷新节奏由频道级调度负责）")
    finally:
        ExtBot.send_message = original_send
        ExtBot.edit_message_text = original_edit
        CallbackQuery.answer = original_answer
        await app.shutdown()
        if app.post_shutdown is not None:
            await app.post_shutdown(app)
    print("\n✅ 端到端调试完成")


if __name__ == "__main__":
    asyncio.run(main())
