"""展示与消息合成：模板摘要、推送摘要、Telegram 分块。"""
from __future__ import annotations

import json

from .models import Post, Template, format_answer_value

_CHUNK_MARK = "（{i}/{n}）"
_TEST_MARK = "🧪 试跑样张（非正式推送）\n\n"


def _fmt_entry(value: object) -> str:
    """criteria 条目渲染：字符串原样；结构化对象/数组压成紧凑 JSON；None → —。"""
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def match_summary(template: Template) -> str:
    """把 match 规则渲染成一行人类可读文本。"""
    parts: list[str] = []
    for cond in template.match.conditions:
        question = template.questions.get(cond.question)
        label = (question.title or cond.question) if question else cond.question
        if isinstance(cond.value, list):
            value = ", ".join(str(v) for v in cond.value)
        else:
            value = str(cond.value)
        parts.append(f"{label} {cond.op} {value}")
    return (" 且 " if template.match.logic == "all" else " 或 ").join(parts) or "（未定义）"


def template_summary(template: Template) -> str:
    """给用户确认用的模板全貌。"""
    lines = [f"🧩 模板：{template.name or '（未命名）'}", ""]
    for index, (qid, question) in enumerate(template.questions.items(), 1):
        lines.append(f"{index}. 「{question.title or qid}」（{question.type}）")
        instructions = question.instructions
        if not isinstance(instructions, str):
            instructions = json.dumps(instructions, ensure_ascii=False)
        lines.append(f"   问题：{instructions}")
        if question.type == "noul" and question.criteria:
            lines.append(f"   是：{_fmt_entry(question.criteria.get('true'))}")
            lines.append(f"   否：{_fmt_entry(question.criteria.get('false'))}")
        elif question.type == "choice":
            lines.append("   选项：" + " / ".join(question.criteria.keys()))
        elif question.type == "score":
            lines.append("   等级：" + " < ".join(
                _fmt_entry(item) for item in question.criteria))
    lines.append("")
    lines.append(f"🎯 命中条件：{match_summary(template)}")
    return "\n".join(lines)


def compose_digest(source: str, sub_id: int, hits: list[tuple[Post, dict]],
                   template: Template, chunk_limit: int = 3800,
                   test: bool = False) -> list[str]:
    """命中列表 → 一条或多条可直接发送的消息文本；test=True 时首条加试跑标头。"""
    if not hits:
        return []
    span = ""
    dates = sorted(p.date for p, _ in hits if p.date)
    if dates:
        span = f"｜{dates[0].strftime('%m-%d %H:%M')}–{dates[-1].strftime('%H:%M')}"
    header = f"📮 @{source}｜订阅 #{sub_id}{span}｜命中 {len(hits)} 条"

    blocks: list[str] = []
    for index, (post, answers) in enumerate(hits, 1):
        timestamp = post.date.strftime("%m-%d %H:%M") if post.date else "?"
        values = "｜".join(
            f"{q.title or qid} {format_answer_value(q, answers.get(qid))}"
            for qid, q in template.questions.items())
        blocks.append(f"{index}. {timestamp}｜{values}\n{post.text}\n🔗 {post.url}")

    chunks = _chunk_blocks(header, blocks, chunk_limit)
    if len(chunks) > 1:  # 多段时加（i/n）标记
        total = len(chunks)
        chunks = [f"{chunk}\n\n{_CHUNK_MARK.format(i=i, n=total)}"
                  for i, chunk in enumerate(chunks, 1)]
    if test:
        chunks[0] = f"{_TEST_MARK}{chunks[0]}"
    return chunks


def _chunk_blocks(header: str, blocks: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = header
    for block in blocks:
        while True:
            candidate = f"{current}\n\n{block}" if current else block
            if len(candidate) <= limit:
                current = candidate
                break
            if current:
                chunks.append(current)
                current = ""
                continue
            # 单块自身超限：硬切
            chunks.append(block[:limit])
            block = block[limit:]
            if not block:
                break
    if current:
        chunks.append(current)
    return chunks
