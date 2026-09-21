"""Shared fixtures and helpers for the test suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Constructors re-exported for direct import from tests
from tgfilter.models import Template  # noqa: E402


def make_page(channel: str, ids: list[int], title: str = "测试频道",
              text: str | None = None) -> str:
    """Build minimal preview HTML matching the t.me/s/ structure."""
    blocks = []
    for mid in ids:
        body = text if text is not None else f"第 {mid} 条<br/>内容 &amp; 更多"
        blocks.append(
            f'<div class="tgme_widget_message js-widget_message" '
            f'data-post="{channel}/{mid}">'
            f'<div class="tgme_widget_message_text js-message_text">{body}</div>'
            f'<time datetime="2026-09-20T12:{mid % 60:02d}:00+00:00"></time>'
            f"</div>")
    return (f'<html><head><meta property="og:title" content="{title}"/></head>'
            f"<body>{''.join(blocks)}</body></html>")


def make_template(**overrides) -> Template:
    data = {
        "name": "中国",
        "questions": {
            "china": {"type": "noul", "title": "相关",
                      "instructions": "是否与中国相关？"},
        },
        "match": {"logic": "all",
                  "conditions": [{"question": "china", "op": ">=", "value": 0.7}]},
    }
    data.update(overrides)
    return Template.model_validate(data)


@pytest.fixture
def page_factory():
    return make_page


@pytest.fixture
def template():
    return make_template()
