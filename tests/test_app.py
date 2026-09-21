"""app 层：自定义并发处理器（全局并发 + 每聊天串行）。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telegram import Chat, Message, Update, User

from tgfilter.bot.app import PerChatUpdateProcessor


def _upd(update_id: int, chat_id: int) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=1, first_name="u", is_bot=False), text="x"))


async def test_same_chat_updates_run_in_order():
    """同一聊天的更新严格串行（先后顺序保持）。"""
    proc = PerChatUpdateProcessor(max_concurrent_updates=4)
    await proc.initialize()
    ran: list[str] = []

    async def job(tag: str, delay: float) -> None:
        ran.append(f"{tag}-start")
        await asyncio.sleep(delay)
        ran.append(f"{tag}-end")

    await asyncio.gather(
        proc.process_update(_upd(1, 100), job("a", 0.05)),
        proc.process_update(_upd(2, 100), job("b", 0.01)),
    )
    assert ran == ["a-start", "a-end", "b-start", "b-end"]


async def test_different_chats_run_concurrently():
    """不同聊天的更新并行执行（互不阻塞）。"""
    proc = PerChatUpdateProcessor(max_concurrent_updates=4)
    await proc.initialize()
    ran: list[str] = []

    async def job(tag: str, delay: float) -> None:
        ran.append(f"{tag}-start")
        await asyncio.sleep(delay)
        ran.append(f"{tag}-end")

    await asyncio.gather(
        proc.process_update(_upd(1, 200), job("c", 0.05)),
        proc.process_update(_upd(2, 300), job("d", 0.01)),
    )
    assert ran == ["c-start", "d-start", "d-end", "c-end"]
