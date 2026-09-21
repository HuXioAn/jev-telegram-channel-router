"""Delivery layer: rate-limit retries and error mapping."""
from __future__ import annotations

from datetime import timedelta

import pytest
from telegram.error import Forbidden, RetryAfter

from tgfilter.delivery import DeliveryError, Sender


class FlakyBot:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.attempts = 0
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RetryAfter(retry_after=timedelta(seconds=0))
        self.sent.append(text)


class BlockedBot:
    async def send_message(self, chat_id, text, **kwargs):
        raise Forbidden("Forbidden: bot was blocked by the user")


async def test_sender_retries_after_rate_limit():
    bot = FlakyBot(failures=1)
    await Sender(bot).send(1, ["hello"])
    assert bot.attempts == 2
    assert bot.sent == ["hello"]


async def test_sender_maps_forbidden_to_delivery_error():
    with pytest.raises(DeliveryError):
        await Sender(BlockedBot()).send(1, ["x"])
