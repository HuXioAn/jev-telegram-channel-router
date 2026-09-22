"""User-facing message composition.

Every string is pulled from tgfilter.i18n by language code; this module only
assembles dynamic values (counts, names, timestamps) around them.
"""
from __future__ import annotations

from ..formatting import match_summary
from ..i18n import t
from ..models import TELEGRAM_TEXT_LIMIT, clip_text, format_answer_value


_MAX_EXAMPLES = 8  # example lines on a /test result page (the rest is folded)


# ------------------------------------------------------------------ general
def welcome(lang: str) -> str:
    return t(lang, "welcome")


def help_text(lang: str) -> str:
    return t(lang, "help")


def fallback(lang: str) -> str:
    return t(lang, "fallback")


def unknown_cmd(lang: str) -> str:
    return t(lang, "unknown_cmd")


def canceled(lang: str) -> str:
    return t(lang, "canceled")


def error(lang: str, err: object) -> str:
    return t(lang, "error", err=err)


def unconfigured(lang: str) -> str:
    return t(lang, "unconfigured")


def blocked(lang: str) -> str:
    return t(lang, "blocked")


def private_only(lang: str, cmd: str) -> str:
    return t(lang, "private_only", cmd=cmd)


def new_private_only(lang: str) -> str:
    return t(lang, "new_private_only")


def toast_refreshed(lang: str) -> str:
    return t(lang, "toast_refreshed")


def lang_prompt(lang: str, current: str) -> str:
    return t(lang, "lang_prompt", current=current)


def lang_set(lang: str) -> str:
    return t(lang, "lang_set")


# -------------------------------------------------------------- /new wizard
def ask_source(lang: str) -> str:
    return t(lang, "ask_source")


def bad_source(lang: str, err: object) -> str:
    return t(lang, "bad_source", err=err)


def source_unreachable(lang: str, err: object) -> str:
    return t(lang, "source_unreachable", err=err)


def source_duplicate(lang: str) -> str:
    return t(lang, "source_duplicate")


def need_one_source(lang: str) -> str:
    return t(lang, "need_one_source")


def _join(lang: str, items: list[str], cap: int = 3) -> str:
    if not items:
        return t(lang, "none_label")
    sep = t(lang, "list_sep")
    shown = sep.join(items[:cap])
    return shown + (t(lang, "more_items", n=len(items)) if len(items) > cap else "")


def src_manager_text(lang: str, sources: list[str], *, next_step: bool) -> str:
    body = "\n".join(f"· @{s}" for s in sources) or t(lang, "none_label")
    hint = t(lang, "src_manager_next" if next_step else "src_manager_back")
    return f"{t(lang, 'src_manager_head')}\n\n{body}\n\n{hint}"


def ask_describe(lang: str) -> str:
    return t(lang, "ask_describe")


def compiling(lang: str) -> str:
    return t(lang, "compiling")


def compile_failed(lang: str, err: object) -> str:
    return t(lang, "compile_failed", err=err)


def llm_not_configured(lang: str) -> str:
    return t(lang, "llm_not_configured")


def bad_template_json(lang: str, err: object) -> str:
    return t(lang, "bad_template_json", err=err)


def template_confirm(lang: str, summary: str, *, edit: bool = False) -> str:
    key = "template_confirm_edit" if edit else "template_confirm"
    return clip_text(t(lang, key, summary=summary), TELEGRAM_TEXT_LIMIT)


def ask_adjust(lang: str) -> str:
    return t(lang, "ask_adjust")


# ------------------------------------------------------------------- targets
def dest_label(lang: str, dest: dict) -> str:
    """Display label for a delivery target (DM gets a localized label)."""
    if dest.get("kind") == "dm":
        return t(lang, "dest_dm_title")
    return dest.get("title") or str(dest.get("chat_id"))


def dest_names(lang: str, dests: list[dict]) -> str:
    return _join(lang, [dest_label(lang, d) for d in dests])


def dest_manager_text(lang: str, dests: list[str], *, next_step: bool) -> str:
    body = "\n".join(f"· {d}" for d in dests) or t(lang, "dest_manager_empty")
    hint = t(lang, "dest_manager_next" if next_step else "dest_manager_back")
    return f"{t(lang, 'dest_manager_head')}\n\n{body}\n\n{hint}"


def dest_duplicate(lang: str) -> str:
    return t(lang, "dest_duplicate")


def need_one_dest(lang: str) -> str:
    return t(lang, "need_one_dest")


def dest_channel_invalid(lang: str, title: str) -> str:
    return t(lang, "dest_channel_invalid", title=title)


def dest_channel_not_yours(lang: str, title: str) -> str:
    return t(lang, "dest_channel_not_yours", title=title)


def dest_user_not_admin(lang: str, title: str) -> str:
    return t(lang, "dest_user_not_admin", title=title)


def sub_created(lang: str, sub_id: int, sources: str, rule: str, dests: str) -> str:
    return t(lang, "sub_created", sub_id=sub_id, sources=sources, rule=rule, dests=dests)


def subs_limit(lang: str, n: int) -> str:
    return t(lang, "subs_limit", n=n)


# ---------------------------------------------------------------- /list view
def no_subs(lang: str) -> str:
    return t(lang, "no_subs")


def list_head(lang: str, n: int) -> str:
    return t(lang, "list_head", n=n)


def list_hint(lang: str) -> str:
    return t(lang, "list_hint")


def sub_line(lang: str, sub: dict, template) -> str:
    status = t(lang, "status_running" if sub["enabled"] else "status_paused")
    sources = _join(lang, [f"@{s['source']}" for s in sub["sources"]])
    dests = dest_names(lang, sub["dests"])
    return t(lang, "sub_line_format", id=sub["id"], sources=sources, dests=dests,
             status=status, rule_label=t(lang, "rule_label"),
             rule=match_summary(lang, template))


def sub_pick_line(lang: str, index: int, sub: dict) -> str:
    """One row in the selection view (short: first target only)."""
    state = t(lang, "status_running" if sub["enabled"] else "status_paused")
    sources = _join(lang, [f"@{s['source']}" for s in sub["sources"]], cap=2)
    first = sub["dests"][0] if sub["dests"] else None
    dests = dest_label(lang, first) if first else t(lang, "none_label")
    if len(sub["dests"]) > 1:
        dests += t(lang, "pick_more", n=len(sub["dests"]) - 1)
    return t(lang, "pick_line_format", index=index, id=sub["id"],
             sources=sources, dests=dests, state=state)


def sub_deleted(lang: str, sub_id: int) -> str:
    return t(lang, "sub_deleted", sub_id=sub_id)


# -------------------------------------------------------------------- /test
def test_need_id(lang: str) -> str:
    return t(lang, "test_need_id")


def test_sub_not_found(lang: str, sub_id: object) -> str:
    return t(lang, "test_sub_not_found", sub_id=sub_id)


def testing(lang: str) -> str:
    return t(lang, "testing")


def test_result(lang: str, res, sub: dict, template) -> str:
    lines = [
        t(lang, "test_result_title", sub_id=res.sub_id),
        t(lang, "test_sources", sources=_join(lang, [f"@{s['source']}" for s in sub["sources"]])),
        t(lang, "test_stats", fetched=res.fetched, matched=res.matched, failed=res.failed),
    ]
    if res.sent:
        lines.append(t(lang, "test_sent", n=len(res.sample), dests=dest_names(lang, sub["dests"])))
    if res.error:
        lines.append(f"⚠️ {res.error}")
    if res.sample:
        lines.append("")
        lines.append(t(lang, "test_examples_head"))
        for source, post, answers in res.sample[:_MAX_EXAMPLES]:
            timestamp = post.date.strftime("%m-%d %H:%M") if post.date else "?"
            values = "｜".join(
                f"{question.title or qid} {format_answer_value(question, answers.get(qid))}"
                for qid, question in template.questions.items())
            snippet = post.text.replace("\n", " ")[:80]
            lines.append(t(lang, "test_line", source=source, timestamp=timestamp,
                            values=values, snippet=snippet))
        if len(res.sample) > _MAX_EXAMPLES:
            lines.append(t(lang, "more_items", n=len(res.sample) - _MAX_EXAMPLES))
    elif not res.error:
        lines.append("")
        lines.append(t(lang, "test_none"))
    return clip_text("\n".join(lines), TELEGRAM_TEXT_LIMIT)


# -------------------------------------------------------------------- edit
def edit_title(lang: str, sub_id: int) -> str:
    return t(lang, "edit_title", sub_id=sub_id)


def edit_menu_text(lang: str, sub: dict, template) -> str:
    return f"{edit_title(lang, sub['id'])}\n\n{sub_line(lang, sub, template)}\n\n{t(lang, 'edit_hint')}"


def edit_done(lang: str) -> str:
    return t(lang, "edit_done")


def edit_template_ask(lang: str) -> str:
    return t(lang, "edit_template_ask")


def edit_source_ask(lang: str) -> str:
    return t(lang, "edit_source_ask")


# ------------------------------------------------------------------- misc
def chat_added(lang: str, title: str) -> str:
    return t(lang, "chat_added", title=title)
