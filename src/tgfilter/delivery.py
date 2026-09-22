"""Delivery layer: send digest messages to a DM or channel."""
from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

logger = logging.getLogger(__name__)

_SEND_ATTEMPTS = 3


class DeliveryError(Exception):
    pass


class Sender:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def send(self, chat_id: int, chunks: list[str]) -> None:
        for chunk in chunks:
            await self._send_one(chat_id, chunk)

    async def _send_one(self, chat_id: int, text: str) -> None:
        for attempt in range(1, _SEND_ATTEMPTS + 1):
            try:
                await self._bot.send_message(chat_id=chat_id, text=text,
                                             parse_mode="HTML")
                return
            except RetryAfter as exc:  # Telegram rate limit: wait as told, then retry
                seconds = float(getattr(exc.retry_after, "total_seconds",
                                        exc.retry_after)) + 0.5
                if attempt >= _SEND_ATTEMPTS:
                    raise DeliveryError(
                        f"rate limited (still failing after {attempt} attempts, "
                        f"{seconds:.0f}s waits)") from exc
                logger.warning("send rate-limited, retrying in %.1fs (%s/%s)",
                               seconds, attempt, _SEND_ATTEMPTS)
                await asyncio.sleep(seconds)
            except Forbidden as exc:
                raise DeliveryError(
                    "cannot post: the user never started the bot, or the bot is "
                    "not a member of the target channel") from exc
            except BadRequest as exc:
                raise DeliveryError(f"rejected: {exc.message}") from exc
            except TelegramError as exc:
                raise DeliveryError(f"send failed: {exc}") from exc
