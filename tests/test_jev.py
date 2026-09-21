"""Jev client: success, rate-limit retry, failure short-circuit, concurrency order."""
from __future__ import annotations

import httpx
import pytest

from conftest import make_template
from tgfilter.jev import JevClient


class _NoSleep:
    async def __call__(self, _seconds: float) -> None:
        return None


def _client(handler, concurrency: int = 4) -> tuple[JevClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = JevClient(http, "test-key", "https://jev.test/v1", concurrency=concurrency)
    client._sleep = _NoSleep()
    return client, http


async def test_classify_success():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={
            "answers": {"china": {"type": "noul", "noul": 0.93}},
            "usage": {"total_tokens": 42}})

    client, http = _client(handler)
    try:
        result = await client.classify("消息文本", make_template())
    finally:
        await http.aclose()
    assert result["answers"]["china"]["noul"] == 0.93
    assert result["usage"]["total_tokens"] == 42
    assert seen["url"] == "https://jev.test/v1/systemone"
    assert seen["auth"] == "Bearer test-key"
    import json
    body = json.loads(seen["body"])
    assert body["state"] == "消息文本"
    assert "title" not in body["questions"]["china"]


async def test_classify_retries_on_429():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"answers": {}, "usage": {}})

    client, http = _client(handler)
    try:
        result = await client.classify("x", make_template())
    finally:
        await http.aclose()
    assert "error" not in result
    assert calls["n"] == 2


async def test_classify_short_circuits_on_client_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(422, text="bad request")

    client, http = _client(handler)
    try:
        result = await client.classify("x", make_template())
    finally:
        await http.aclose()
    assert "error" in result and "422" in result["error"]
    assert calls["n"] == 1  # non-rate-limit errors are not retried


async def test_classify_many_preserves_order_and_isolates_failures():
    def handler(request):
        import json
        state = json.loads(request.content)["state"]
        if state == "bad":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"answers": {"n": state}, "usage": {}})

    client, http = _client(handler)
    try:
        results = await client.classify_many(["a", "bad", "c"], make_template())
    finally:
        await http.aclose()
    assert results[0]["answers"]["n"] == "a"
    assert "error" in results[1]
    assert results[2]["answers"]["n"] == "c"
