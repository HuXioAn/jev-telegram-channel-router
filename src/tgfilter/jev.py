"""Jev (TypeSafe System One) client: classifies items one by one, with concurrency + backoff retries."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .models import Template


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, base_url: str,
                 concurrency: int = 8, timeout: float = 60.0):
        self._http = http
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._sem = asyncio.Semaphore(concurrency)
        self._timeout = timeout
        self._sleep = asyncio.sleep  # injectable (tests)

    async def classify(self, text: str, template: Template) -> dict[str, Any]:
        """Classify one message (per the template's question set). Returns {"answers": {...}, "usage": {...}}; on failure {"error": ...}."""
        return await self.classify_questions(text, template.jev_questions())

    async def classify_questions(self, text: str,
                                 questions: dict[str, dict]) -> dict[str, Any]:
        """Classify one message against a given question set (the low-level call behind union judging)."""
        payload = {
            "state": text,
            "model": "jev-latest",
            "questions": questions,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with self._sem:
            for attempt in range(3):
                try:
                    response = await self._http.post(
                        f"{self._base}/systemone", json=payload,
                        headers=headers, timeout=self._timeout)
                    if response.status_code == 200:
                        data = response.json()
                        return {"answers": data.get("answers", {}),
                                "usage": data.get("usage", {})}
                    if response.status_code in (429, 529):  # rate limited / overloaded: back off and retry
                        await self._sleep(1.5 * (2 ** attempt))
                        continue
                    return {"error": f"HTTP {response.status_code}: {response.text[:200]}"}
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        return {"error": f"{type(exc).__name__}: {exc}"}
                    await self._sleep(1.0 * (attempt + 1))
        return {"error": "重试耗尽"}

    async def classify_many(self, texts: list[str], template: Template) -> list[dict[str, Any]]:
        """Classify a batch of messages concurrently (order matches input; one failure does not affect the others)."""
        return list(await asyncio.gather(*(self.classify(t, template) for t in texts)))
