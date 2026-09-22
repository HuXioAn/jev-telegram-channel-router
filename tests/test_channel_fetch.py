"""Channel fetching: parsing, normalization, paged resume, error paths."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import make_page
from tgfilter.channel_fetch import (ChannelError, ChannelFetcher, normalize_channel_ref,
                                    parse_posts, parse_title)


# ------------------------------------------------------------ ref normalization
@pytest.mark.parametrize("raw,expected", [
    ("@Financial_Express", "Financial_Express"),
    ("Financial_Express", "Financial_Express"),
    ("https://t.me/Financial_Express", "Financial_Express"),
    ("http://t.me/Financial_Express/", "Financial_Express"),
    ("t.me/s/Financial_Express", "Financial_Express"),
    ("https://t.me/Financial_Express/3477123", "Financial_Express"),
    ("telegram.me/Financial_Express", "Financial_Express"),
])
def test_normalize_ok(raw, expected):
    assert normalize_channel_ref(raw) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "+AbCdEf123", "https://t.me/+AbCdEf123",
    "https://t.me/joinchat/AbCdEf", "a b", "https://t.me/", "t.me/s",
])
def test_normalize_rejects(bad):
    with pytest.raises(ValueError):
        normalize_channel_ref(bad)


# ------------------------------------------------------------------ parsing
def test_parse_posts():
    html = make_page("chan", [101, 102])
    posts = parse_posts(html, "chan")
    assert [p.id for p in posts] == [101, 102]
    assert posts[0].text == "第 101 条\n内容 & 更多"  # <br/> → \n, entity decoded
    assert posts[0].url == "https://t.me/chan/101"
    assert posts[0].date is not None and posts[0].date.year == 2026


def test_parse_posts_empty():
    assert parse_posts("<html><body>nothing</body></html>", "chan") == []


def test_parse_posts_truncates_long_text():
    """Overlong posts are clipped at fetch time: Jev judges and users receive the
    clipped text + the link to the full post, never the whole blob."""
    html = make_page("chan", [101], text="长" * 5000)
    post = parse_posts(html, "chan", max_chars=100)[0]
    assert len(post.text) == 100 and post.text.endswith("…")
    assert post.url == "https://t.me/chan/101"
    assert len(parse_posts(html, "chan")[0].text) <= 3500 + 1  # default cap applies


def test_parse_title():
    assert parse_title(make_page("chan", [1], title="财经快讯")) == "财经快讯"
    assert parse_title("<html></html>") == ""


# ------------------------------------------------------------------ fetching
def _noop_sleep(_seconds: float):
    async def run():
        return None
    return run()


def _fetcher(handler, **kwargs) -> tuple[ChannelFetcher, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = ChannelFetcher(http, page_delay=0, **kwargs)
    fetcher._sleep = lambda seconds: _noop_sleep(seconds)
    return fetcher, http


async def test_head_returns_info():
    def handler(request):
        assert str(request.url) == "https://t.me/s/chan"
        return httpx.Response(200, text=make_page("chan", [41, 42, 43], title="财经快讯"))

    fetcher, http = _fetcher(handler)
    try:
        info = await fetcher.head("chan")
    finally:
        await http.aclose()
    assert info.title == "财经快讯"
    assert info.head_id == 43
    assert len(info.posts) == 3


async def test_head_rejects_channel_without_preview():
    def handler(request):
        return httpx.Response(200, text="<html><body>no posts here</body></html>")

    fetcher, http = _fetcher(handler)
    with pytest.raises(ChannelError):
        await fetcher.head("chan")
    await http.aclose()


async def test_fetch_since_paginates_until_empty():
    def handler(request):
        after = int(request.url.params.get("after", "0"))
        ids = [i for i in range(1, 46) if i > after][:20]
        return httpx.Response(200, text=make_page("chan", ids))

    fetcher, http = _fetcher(handler)
    try:
        posts, cursor = await fetcher.fetch_since("chan", 0)
    finally:
        await http.aclose()
    assert [p.id for p in posts] == list(range(1, 46))
    assert cursor == 45


async def test_fetch_since_noop_when_caught_up():
    def handler(request):
        return httpx.Response(200, text=make_page("chan", [100, 101]))

    fetcher, http = _fetcher(handler)
    try:
        posts, cursor = await fetcher.fetch_since("chan", 101)
    finally:
        await http.aclose()
    assert posts == []
    assert cursor == 101


async def test_get_retries_on_transient_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, text=make_page("chan", [7]))

    fetcher, http = _fetcher(handler)
    try:
        info = await fetcher.head("chan")
    finally:
        await http.aclose()
    assert info.head_id == 7
    assert calls["n"] == 2


async def test_get_raises_after_retries():
    def handler(request):
        return httpx.Response(503, text="down")

    fetcher, http = _fetcher(handler)
    with pytest.raises(ChannelError):
        await fetcher.head("chan")
    await http.aclose()
