"""投递层：把摘要消息发给 DM 或频道。"""
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
                await self._bot.send_message(chat_id=chat_id, text=text)
                return
            except RetryAfter as exc:  # Telegram 限流：按提示等待后重试
                seconds = float(getattr(exc.retry_after, "total_seconds",
                                        exc.retry_after)) + 0.5
                if attempt >= _SEND_ATTEMPTS:
                    raise DeliveryError(
                        f"发送被限流（等待 {seconds:.0f}s 重试 {attempt} 次仍失败）") from exc
                logger.warning("发送被限流，等待 %.1fs 后重试（%s/%s）",
                               seconds, attempt, _SEND_ATTEMPTS)
                await asyncio.sleep(seconds)
            except Forbidden as exc:
                raise DeliveryError(
                    "无权限发送：用户未与机器人对话，或机器人不在目标频道") from exc
            except BadRequest as exc:
                raise DeliveryError(f"发送被拒：{exc.message}") from exc
            except TelegramError as exc:
                raise DeliveryError(f"发送失败：{exc}") from exc
