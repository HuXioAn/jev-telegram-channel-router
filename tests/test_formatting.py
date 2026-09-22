"""Digest composition and chunking (all rendering is language-aware)."""
from __future__ import annotations

from datetime import datetime, timezone

from conftest import make_template
from tgfilter.formatting import compose_digest, match_summary, template_summary
from tgfilter.models import Post, Template


def test_test_result_page_bounded():
    """A multi-source dry-run must never overflow the Telegram text limit
    (unbounded example lists used to raise BadRequest: Message_too_long)."""
    from types import SimpleNamespace

    from tgfilter.bot import messages as msg

    res = SimpleNamespace(
        sub_id=11, fetched=60, matched=60, failed=0, sent=False, error="",
        sample=[("chan", Post(id=i, date=datetime(2026, 9, 22, 10, 0),
                              text="文" * 3500, url=f"https://t.me/chan/{i}"),
                 {"china": {"type": "noul", "noul": 0.9}})
                for i in range(40)])
    sub = {"id": 11, "enabled": 1,
           "sources": [{"source": "chan"}, {"source": "other"}],
           "dests": [{"kind": "dm", "chat_id": 1, "title": "私聊"}]}
    text = msg.test_result("zh", res, sub, make_template())
    assert len(text) <= 4000
    assert "等 32 个" in text          # 40 examples, 8 shown, the rest folded


def _post(mid: int, text: str = "内容", hour: int = 10) -> Post:
    return Post(id=mid, text=text, url=f"https://t.me/chan/{mid}",
                channel="chan", channel_title="测试频道",
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


def test_compose_digest_text_plus_link_line():
    """Each block: post text + one footer line — hyperlinked post link | source
    name (linked to the channel), and nothing else."""
    hits = [(_post(1, "第一条消息", 10), _answers()),
            (_post(2, "第二条消息", 12), _answers())]

    def footer(mid: int, label: str) -> str:
        return (f'<a href="https://t.me/chan/{mid}">{label}</a> | '
                f'<a href="https://t.me/chan">测试频道</a>')

    chunks = compose_digest(hits)
    assert chunks == ["第一条消息\n" + footer(1, "Original post") +
                      "\n\n第二条消息\n" + footer(2, "Original post")]
    zh = compose_digest(hits, lang="zh")
    assert zh[0] == ("第一条消息\n" + footer(1, "原文链接") +
                     "\n\n第二条消息\n" + footer(2, "原文链接"))
    # header and per-item prefixes are gone
    assert "📮" not in chunks[0] and "订阅 #" not in chunks[0]
    assert "命中" not in chunks[0] and "09-20" not in chunks[0]
    assert not chunks[0].startswith("1.")


def test_compose_digest_multi_chunk_marker_localized():
    hits = [(_post(i, "x" * 900), _answers()) for i in range(1, 4)]
    en = compose_digest(hits, chunk_limit=2000)
    assert len(en) == 2
    assert all(len(c) <= 2010 for c in en)
    for i in range(1, 4):   # every post link appears exactly once across chunks
        assert sum(c.count(f'href="https://t.me/chan/{i}"') for c in en) == 1
    assert "(1/2)" in en[0] and "(2/2)" in en[1]
    zh = compose_digest(hits, chunk_limit=2000, lang="zh")
    assert "（1/2）" in zh[0] and "（2/2）" in zh[1]


def test_compose_digest_escapes_html_and_clips_oversized_block():
    """HTML is escaped; an oversized block is clipped to the budget (the link
    always carries the full post) instead of split into fragments."""
    hits = [(_post(1, "<b>重点</b> & " + "y" * 5000), _answers())]
    chunks = compose_digest(hits, chunk_limit=1000)
    assert len(chunks) == 1
    assert len(chunks[0]) <= 1000
    assert "&lt;b&gt;重点&lt;/b&gt; &amp;" in chunks[0]
    assert chunks[0].count('href="https://t.me/chan/1"') == 1
    assert chunks[0].endswith("</a>")


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
