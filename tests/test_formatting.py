"""Digest composition and chunking (all rendering is language-aware)."""
from __future__ import annotations

from datetime import datetime, timezone

from conftest import make_template
from tgfilter.formatting import compose_digest, match_summary, template_summary
from tgfilter.models import Post, Template


def _post(mid: int, text: str = "内容", hour: int = 10) -> Post:
    return Post(id=mid, text=text, url=f"https://t.me/chan/{mid}",
                date=datetime(2026, 9, 20, hour, 0, tzinfo=timezone.utc))


def _answers(score: float = 0.9) -> dict:
    return {"china": {"type": "noul", "noul": score}}


def test_match_summary_uses_titles():
    assert match_summary("en", make_template()) == "相关 >= 0.7"


def test_match_summary_localized_joiners_and_undefined():
    tpl = make_template(
        questions={
            "a": {"type": "noul", "title": "A", "instructions": "a?"},
            "b": {"type": "noul", "title": "B", "instructions": "b?"}},
        match={"logic": "any", "conditions": [
            {"question": "a", "op": ">=", "value": 0.5},
            {"question": "b", "op": ">=", "value": 0.5}]})
    assert " OR " in match_summary("en", tpl)
    assert " 或 " in match_summary("zh", tpl)
    empty = Template.model_validate({
        "name": "x",
        "questions": {"a": {"type": "noul", "instructions": "a?"}},
        "match": {"logic": "all", "conditions": []}})
    assert match_summary("en", empty) == "(undefined)"
    assert match_summary("zh", empty) == "（未定义）"


def test_template_summary_sections_en():
    text = template_summary("en", make_template())
    assert "1. “相关” (noul)" in text
    assert "Question: 是否与中国相关？" in text
    assert "🎯 Match condition: 相关 >= 0.7" in text


def test_template_summary_sections_zh():
    text = template_summary("zh", make_template())
    assert "1. 「相关」（noul）" in text
    assert "问题：是否与中国相关？" in text
    assert "🎯 命中条件：相关 >= 0.7" in text


def test_template_summary_renders_structured_entries():
    tpl = make_template(questions={
        "china": {"type": "noul", "title": "相关", "instructions": "是否与中国相关？",
                  "criteria": {"true": {"what": "涉及中国"}, "false": "纯海外"}},
        "imp": {"type": "score", "title": "重要", "instructions": "有多重要？",
                "criteria": [{"summary": "低"}, "高"]}})
    text = template_summary("en", tpl)
    assert '{"what":"涉及中国"}' in text
    assert '{"summary":"低"}' in text


def test_compose_digest_content_and_link_only():
    hits = [(_post(1, "第一条消息", 10), _answers()),
            (_post(2, "第二条消息", 12), _answers())]
    chunks = compose_digest(hits)
    assert len(chunks) == 1
    assert chunks[0] == ("第一条消息\nhttps://t.me/chan/1\n\n"
                         "第二条消息\nhttps://t.me/chan/2")
    # header and per-item prefixes are gone
    assert "📮" not in chunks[0] and "订阅 #" not in chunks[0]
    assert "命中" not in chunks[0] and "09-20" not in chunks[0]
    assert not chunks[0].startswith("1.")


def test_compose_digest_multi_chunk_marker_localized():
    hits = [(_post(i, "x" * 900), _answers()) for i in range(1, 4)]
    en = compose_digest(hits, chunk_limit=2000)
    assert len(en) == 2
    assert all(len(c) <= 2010 for c in en)
    assert sum(c.count("https://t.me/chan/") for c in en) == 3
    assert "(1/2)" in en[0] and "(2/2)" in en[1]
    zh = compose_digest(hits, chunk_limit=2000, lang="zh")
    assert "（1/2）" in zh[0] and "（2/2）" in zh[1]


def test_compose_digest_hard_split_for_oversized_block():
    hits = [(_post(1, "y" * 5000), _answers())]
    chunks = compose_digest(hits, chunk_limit=1000)
    assert len(chunks) >= 5
    assert all(len(c) <= 1010 for c in chunks)
    assert sum(c.count("https://t.me/chan/1") for c in chunks) == 1


def test_compose_digest_empty():
    assert compose_digest([]) == []


def test_compose_digest_test_marker_on_first_chunk_only():
    hits = [(_post(i, "x" * 900), _answers()) for i in range(1, 4)]
    en = compose_digest(hits, chunk_limit=2000, test=True)
    assert len(en) == 2
    assert en[0].startswith("🧪 Sample (not a real delivery)")
    assert "🧪" not in en[1]
    zh = compose_digest(hits, chunk_limit=2000, test=True, lang="zh")
    assert zh[0].startswith("🧪 试跑样张（非正式推送）")
    assert "🧪" not in zh[1]
