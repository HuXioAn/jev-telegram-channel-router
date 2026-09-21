"""Jev（TypeSafe System One）客户端：逐条分类，并发 + 退避重试。"""
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
        self._sleep = asyncio.sleep  # 可注入（测试）

    async def classify(self, text: str, template: Template) -> dict[str, Any]:
        """判定一条消息（按模板问题集）。返回 {"answers": {...}, "usage": {...}}，失败为 {"error": ...}。"""
        return await self.classify_questions(text, template.jev_questions())

    async def classify_questions(self, text: str,
                                 questions: dict[str, dict]) -> dict[str, Any]:
        """按给定问题集判定一条消息（联合判定的底层调用）。"""
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
                    if response.status_code in (429, 529):  # 限流/过载：退避重试
                        await self._sleep(1.5 * (2 ** attempt))
                        continue
                    return {"error": f"HTTP {response.status_code}: {response.text[:200]}"}
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        return {"error": f"{type(exc).__name__}: {exc}"}
                    await self._sleep(1.0 * (attempt + 1))
        return {"error": "重试耗尽"}

    async def classify_many(self, texts: list[str], template: Template) -> list[dict[str, Any]]:
        """并发判定一批消息（顺序与输入一致；单项失败不影响其它）。"""
        return list(await asyncio.gather(*(self.classify(t, template) for t in texts)))
