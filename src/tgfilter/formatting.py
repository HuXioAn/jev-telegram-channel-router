"""Presentation helpers: template summaries, digests, Telegram chunking."""
from __future__ import annotations

import json

from .i18n import t
from .models import Post, Template


def _fmt_entry(value: object) -> str:
    """Render a criteria entry: strings as-is; structured values as compact JSON; None → —."""
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def match_summary(lang: str, template: Template) -> str:
    """Render the match rule as one human-readable line."""
    parts: list[str] = []
    for cond in template.match.conditions:
        question = template.questions.get(cond.question)
        label = (question.title or cond.question) if question else cond.question
        if isinstance(cond.value, list):
            value = ", ".join(str(v) for v in cond.value)
        else:
            value = str(cond.value)
        parts.append(f"{label} {cond.op} {value}")
    joiner = t(lang, "fmt_and" if template.match.logic == "all" else "fmt_or")
    return joiner.join(parts) or t(lang, "fmt_undefined")


def template_summary(lang: str, template: Template) -> str:
    """Full template rendering shown to the user for confirmation."""
    lines = [t(lang, "fmt_template_title",
               name=template.name or t(lang, "fmt_unnamed")), ""]
    for index, (qid, question) in enumerate(template.questions.items(), 1):
        lines.append(t(lang, "fmt_q_line", index=index,
                       title=question.title or qid, type=question.type))
        instructions = question.instructions
        if not isinstance(instructions, str):
            instructions = json.dumps(instructions, ensure_ascii=False)
        lines.append(t(lang, "fmt_instructions", text=instructions))
        if question.type == "noul" and question.criteria:
            lines.append(t(lang, "fmt_yes", value=_fmt_entry(question.criteria.get("true"))))
            lines.append(t(lang, "fmt_no", value=_fmt_entry(question.criteria.get("false"))))
        elif question.type == "choice":
            lines.append(t(lang, "fmt_options") + " / ".join(question.criteria.keys()))
        elif question.type == "score":
            lines.append(t(lang, "fmt_levels") + " < ".join(
                _fmt_entry(item) for item in question.criteria))
    lines.append("")
    lines.append(t(lang, "fmt_match", rule=match_summary(lang, template)))
    return "\n".join(lines)


def compose_digest(hits: list[tuple[Post, dict]], *, chunk_limit: int = 3800,
                   test: bool = False, lang: str = "en") -> list[str]:
    """Hit list → one or more ready-to-send message texts.

    Each message holds only the post text plus its link, blocks separated by a
    blank line. Oversized digests are split into chunks (marker at the bottom);
    test=True prepends the dry-run header to the first chunk.
    """
    if not hits:
        return []
    blocks: list[str] = []
    for post, _ in hits:
        blocks.append("\n".join(part for part in (post.text.strip(), post.url)
                                if part))

    chunks = _chunk_blocks(blocks, chunk_limit)
    if len(chunks) > 1:  # add the (i/n) marker when split
        total = len(chunks)
        chunks = [f"{chunk}\n\n{t(lang, 'chunk_mark', i=i, n=total)}"
                  for i, chunk in enumerate(chunks, 1)]
    if test:
        chunks[0] = f"{t(lang, 'test_mark')}{chunks[0]}"
    return chunks


def _chunk_blocks(blocks: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
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
            # a single block over the limit: hard split
            chunks.append(block[:limit])
            block = block[limit:]
            if not block:
                break
    if current:
        chunks.append(current)
    return chunks
