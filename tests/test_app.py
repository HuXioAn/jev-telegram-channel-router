"""app layer: custom concurrency processor (global concurrency + per-chat serialization)."""
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
    """Updates for the same chat run strictly in order (sequencing preserved)."""
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
    """Updates for different chats run concurrently (they do not block each other)."""
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


def test_build_application_disables_link_previews():
    """Link previews are disabled instance-wide (Defaults): messages with links
    must not sprout a preview box below the text."""
    from tgfilter.bot.app import build_application
    from tgfilter.config import Settings

    app = build_application(Settings(bot_token="123456789:" + "A" * 30))
    options = app.bot.defaults.link_preview_options
    assert options is not None and options.is_disabled is True
