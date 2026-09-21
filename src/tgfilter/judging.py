"""Channel-level union judging: merges the questions of several templates into one call, then projects and caches the answers per template.

Design notes (see PLAN §12):
- the same message is sent to Jev only once (question set = union of the questions of all active templates on that channel);
- question-level dedup: identical question payloads (across templates) are asked only once;
- when the per-call question limit is exceeded, shard by template (still far fewer calls than "one call per template");
- results are projected per template and written into the judgments cache, so multiple subscriptions / reruns are naturally idempotent.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .jev import JevClient
from .models import Post, Template
from .store import Store

DEFAULT_MAX_QUESTIONS = 24


def _canon(data: object) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _ask_id(payload: dict) -> str:
    return "q" + hashlib.sha1(_canon(payload).encode()).hexdigest()[:10]


def template_fingerprint(template: Template) -> str:
    """Template judgment fingerprint: only the question set actually sent to Jev (renaming or changing match rules does not affect the cache)."""
    return "t" + hashlib.sha1(_canon(template.jev_questions()).encode()).hexdigest()[:11]


@dataclass
class UnionGroup:
    """The set of questions covered by one Jev call, plus their mapping onto each template's local questions."""
    asked: dict[str, dict]              # asked_id -> question payload
    mapping: dict[str, dict[str, str]]  # fp -> {local qid: asked_id}

    def project(self, answers: dict) -> dict[str, dict[str, Any]]:
        """asked answers -> {fp: {local qid: answer}}."""
        return {fp: {qid: answers.get(aid) for qid, aid in qmap.items()}
                for fp, qmap in self.mapping.items()}


def build_groups(templates: dict[str, Template],
                 max_questions: int = DEFAULT_MAX_QUESTIONS) -> list[UnionGroup]:
    """Arrange {fp: template} into a number of calls; identical question payloads are asked only once per group.

    Sharding only happens when the limit is exceeded (rare); in that case the same question may appear in two shards.
    """
    groups: list[UnionGroup] = []
    asked: dict[str, dict] = {}
    mapping: dict[str, dict[str, str]] = {}

    def flush() -> None:
        nonlocal asked, mapping
        if mapping:
            groups.append(UnionGroup(asked=dict(asked), mapping=mapping))
        asked, mapping = {}, {}

    for fp, template in templates.items():
        questions = list(template.jev_questions().items())
        aids = {qid: _ask_id(payload) for qid, payload in questions}
        fresh = [aid for aid in dict.fromkeys(aids.values()) if aid not in asked]
        if mapping and len(asked) + len(fresh) > max_questions:
            flush()
        for aid in fresh:
            asked[aid] = next(payload for qid, payload in questions
                              if aids[qid] == aid)
        mapping[fp] = aids
    flush()
    return groups


@dataclass
class _PostJudgment:
    payload: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: int = 0
    ok_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


async def judge_post(jev: JevClient, text: str,
                     groups: list[UnionGroup]) -> _PostJudgment:
    """Judge one message per union group; a failing shard does not drag down the remaining shards."""
    out = _PostJudgment()
    for group in groups:
        out.calls += 1
        result = await jev.classify_questions(text, group.asked)
        usage = result.get("usage") or {}
        out.input_tokens += int(usage.get("input_tokens") or 0)
        out.output_tokens += int(usage.get("output_tokens") or 0)
        if result.get("error"):
            continue
        out.ok_calls += 1
        for fp, local in group.project(result.get("answers") or {}).items():
            out.payload.setdefault(fp, {}).update(local)
    return out


@dataclass
class EnsureStats:
    fresh: int = 0        # messages newly judged in this round
    cached: int = 0       # messages served straight from the cache
    calls: int = 0        # actual Jev calls made
    failed: int = 0       # messages whose calls all failed and could not be cached
    input_tokens: int = 0
    output_tokens: int = 0


class JudgeEngine:
    """Cache-first union judging entry point: messages that are missing are judged once with the union question set, then projected and stored."""

    def __init__(self, store: Store, jev: JevClient,
                 max_questions: int = DEFAULT_MAX_QUESTIONS):
        self._store = store
        self._jev = jev
        self._max = max_questions

    async def ensure(self, channel: str, posts: list[Post],
                     templates: dict[str, Template]) -> tuple[dict[int, dict], EnsureStats]:
        """Ensure every post has a cached judgment; returns ({post_id: payload}, stats)."""
        stats = EnsureStats()
        cache = self._store.judgments_for(channel, [p.id for p in posts])
        fresh = [p for p in posts if p.id not in cache]
        stats.cached = len(posts) - len(fresh)
        stats.fresh = len(fresh)
        if not fresh or not templates:
            return cache, stats
        groups = build_groups(templates, self._max)
        results = await asyncio.gather(
            *(judge_post(self._jev, post.text, groups) for post in fresh))
        for post, judged in zip(fresh, results):
            stats.calls += judged.calls
            stats.input_tokens += judged.input_tokens
            stats.output_tokens += judged.output_tokens
            if not judged.payload:
                stats.failed += 1
                continue
            self._store.save_judgment(channel, post.id, judged.payload)
            cache[post.id] = judged.payload
        return cache, stats
