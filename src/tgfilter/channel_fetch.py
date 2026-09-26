"""Public channel fetching: goes through the t.me/s/ web preview (no permissions needed)."""
from __future__ import annotations

import asyncio
import html as html_lib
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from .models import DEFAULT_MAX_POST_CHARS, Post, clip_text

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
_RESERVED = {"joinchat", "c", "addstickers", "share", "proxy", "socks", "iv"}
_BLOCK_SPLIT_RE = re.compile(r'<div class="tgme_widget_message ')
_POST_ID_RE = re.compile(r'data-post="([^"]+)"')
_DATE_RE = re.compile(r'datetime="([^"]+)"')
_TEXT_RE = re.compile(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')


class ChannelError(Exception):
    """Channel is not reachable (missing / web preview disabled / fetch failed)."""


def normalize_channel_ref(ref: str) -> str:
    """Normalize @name / t.me/name / https://t.me/s/name / t.me/name/123 into 'name'."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("频道引用为空")
    ref = re.sub(r"^@", "", ref)
    match = re.match(r"^(?:https?://)?(?:t|telegram)\.me/(.+)$", ref, re.I)
    if match:
        parts = [p for p in match.group(1).strip("/").split("/") if p]
        if parts and parts[0].lower() == "s":
            parts = parts[1:]
        if not parts:
            raise ValueError("无法解析频道链接")
        first = parts[0]
        if first.lower() in _RESERVED or not _USERNAME_RE.match(first):
            raise ValueError("仅支持公开频道（不支持私有/邀请链接）")
        ref = first
    if not _USERNAME_RE.match(ref):
        raise ValueError(f"频道名不合法：{ref!r}")
    return ref


def parse_title(page_html: str) -> str:
    match = _TITLE_RE.search(page_html)
    return html_lib.unescape(match.group(1)) if match else ""


def parse_posts(page_html: str, channel: str,
                max_chars: int = DEFAULT_MAX_POST_CHARS) -> list[Post]:
    """Parse posts out of the preview HTML (structure see tests/fixtures).

    Post text is clipped to max_chars: judging and delivery both work on the
    clipped text, and the link always points at the full post.
    """
    posts: list[Post] = []
    title = parse_title(page_html)  # channel display name (page og:title)
    for block in _BLOCK_SPLIT_RE.split(page_html)[1:]:
        id_match = _POST_ID_RE.search(block)
        if not id_match:
            continue
        try:
            post_id = int(id_match.group(1).rsplit("/", 1)[-1])
        except ValueError:
            continue
        text = ""
        text_match = _TEXT_RE.search(block)
        if text_match:
            raw = re.sub(r"<br\s*/?>", "\n", text_match.group(1))
            raw = re.sub(r"<[^>]+>", "", raw)
            text = html_lib.unescape(raw).strip()
        date: datetime | None = None
        date_match = _DATE_RE.search(block)
        if date_match:
            try:
                date = datetime.fromisoformat(date_match.group(1).replace("Z", "+00:00"))
            except ValueError:
                date = None
        clipped = clip_text(text, max_chars)
        posts.append(Post(id=post_id, date=date, text=clipped,
                          dedupe_text=clip_text(text, 4000),
                          dedupe_truncated=len(text) > 4000,
                          truncated=clipped != text,
                          url=f"https://t.me/{channel}/{post_id}",
                          channel=channel, channel_title=title))
    return posts


@dataclass
class ChannelInfo:
    channel: str
    title: str
    head_id: int
    posts: list[Post]


class ChannelFetcher:
    """Rate-limit-polite fetching; the paging cursor is the "last seen position", so incremental resumes are natural."""

    def __init__(self, http: httpx.AsyncClient, page_delay: float = 0.6,
                 max_pages: int = 200, max_chars: int = DEFAULT_MAX_POST_CHARS):
        self._http = http
        self._delay = page_delay
        self._max_pages = max_pages
        self._max_chars = max_chars
        self._sleep = asyncio.sleep  # injectable (tests)

    async def _get(self, url: str, tries: int = 3) -> str:
        last: object = "unknown"
        for attempt in range(tries):
            try:
                response = await self._http.get(
                    url, headers={"User-Agent": _BROWSER_UA},
                    timeout=45.0, follow_redirects=True)
                if response.status_code == 200:
                    return response.text
                last = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            await self._sleep(1.0 * (attempt + 1))
        raise ChannelError(f"抓取失败 {url}（{last}）")

    async def head(self, channel: str) -> ChannelInfo:
        page_html = await self._get(f"https://t.me/s/{channel}")
        posts = parse_posts(page_html, channel, self._max_chars)
        if not posts:
            raise ChannelError(f"频道 {channel} 不可用或未开启网页预览")
        return ChannelInfo(channel=channel, title=parse_title(page_html) or channel,
                           head_id=max(p.id for p in posts), posts=posts)

    async def fetch_since(self, channel: str, after_id: int) -> tuple[list[Post], int]:
        """Fetch every new post with id > after_id; returns (ascending list, new cursor). An empty window stops it naturally."""
        posts: list[Post] = []
        cursor = after_id
        for _ in range(self._max_pages):
            page = [p for p in parse_posts(
                await self._get(f"https://t.me/s/{channel}?after={cursor}"),
                channel, self._max_chars)
                if p.id > cursor]
            if not page:
                break
            posts.extend(page)
            cursor = max(p.id for p in page)
            await self._sleep(self._delay)
        posts.sort(key=lambda p: p.id)
        return posts, cursor
