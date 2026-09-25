"""Presentation helpers: template summaries and one Telegram message per post."""
from __future__ import annotations

import html
import json
import re

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


def _channel_of(url: str) -> str:
    match = re.match(r"https?://t\.me/([^/]+)/", url or "")
    return match.group(1) if match else ""


def _post_block(post: Post, lang: str, limit: int) -> str:
    """One block: the (escaped) post text + one footer line "post link | source",
    both hyperlinked — the post link opens the post, the source name opens the
    channel. The text is re-clipped so the whole block fits the chunk budget
    (escaping can inflate text, so the cut lands on the escaped string and any
    dangling entity is stripped)."""
    channel = post.channel or _channel_of(post.url)
    name = post.channel_title or (f"@{channel}" if channel else post.url)
    channel_url = f"https://t.me/{channel}" if channel else post.url
    footer = (f'<a href="{html.escape(post.url, quote=True)}">'
              f'{t(lang, "fmt_post_link")}</a> | '
              f'<a href="{html.escape(channel_url, quote=True)}">'
              f'{html.escape(name)}</a>')
    text = html.escape(post.text.strip())
    truncated = post.truncated
    note = t(lang, "truncated_note")
    # budget covers text + "\n" + footer + worst case "\n" + truncation marker,
    # so one block always fits the chunk limit
    budget = limit - len(footer) - len(note) - 2
    if budget > 0 and len(text) > budget:
        text = re.sub(r"&[a-zA-Z#0-9]*$", "", text[:max(0, budget - 1)]) + "…"
        truncated = True
    block = f"{text}\n{footer}" if text else footer
    # truncation marker sits at the very end of the block (after the link footer)
    return f"{block}\n{note}" if truncated else block


def compose_digest(hits: list[tuple[Post, dict]], *, chunk_limit: int = 3800,
                   test: bool = False, lang: str = "en") -> list[str]:
    """Return one independently sendable HTML message per matching source post.

    Keep the existing per-post text and linked footer. A dry-run marks *every*
    message so later examples cannot be mistaken for real deliveries. Reserve
    the marker's space before clipping an oversized post.
    """
    marker = t(lang, "test_mark") if test else ""
    return [marker + _post_block(post, lang, chunk_limit - len(marker))
            for post, _ in hits]
