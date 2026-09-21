"""投递层：把摘要消息发给 DM 或频道。"""
from __future__ import annotations

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError


class DeliveryError(Exception):
    pass


class Sender:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def send(self, chat_id: int, chunks: list[str]) -> None:
        for chunk in chunks:
            try:
                await self._bot.send_message(chat_id=chat_id, text=chunk)
            except Forbidden as exc:
                raise DeliveryError(
                    "无权限发送：用户未与机器人对话，或机器人不在目标频道") from exc
            except BadRequest as exc:
                raise DeliveryError(f"发送被拒：{exc.message}") from exc
            except TelegramError as exc:
                raise DeliveryError(f"发送失败：{exc}") from exc
