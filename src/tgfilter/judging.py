"""频道级联合判定：把多个模板的问题并成一次调用，答案按模板投影后缓存。

设计要点（见 PLAN §12）：
- 同一条消息对 Jev 只调用一次（问题集 = 该频道全部活跃模板的问题并集）；
- 问题级去重：payload 完全相同的提问（跨模板）只问一次；
- 超过单次调用的问题上限时按模板分片（仍远少于「每模板一次」）；
- 结果按模板投影后写入 judgments 缓存，多订阅/重跑天然幂等。
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
    """模板判定指纹：只取实际发给 Jev 的问题集（改名称/命中规则不影响缓存）。"""
    return "t" + hashlib.sha1(_canon(template.jev_questions()).encode()).hexdigest()[:11]


@dataclass
class UnionGroup:
    """一次 Jev 调用覆盖的问题集合及其到各模板本地问题的映射。"""
    asked: dict[str, dict]              # asked_id -> 提问 payload
    mapping: dict[str, dict[str, str]]  # fp -> {本地 qid: asked_id}

    def project(self, answers: dict) -> dict[str, dict[str, Any]]:
        """asked 答案 → {fp: {本地 qid: 答案}}。"""
        return {fp: {qid: answers.get(aid) for qid, aid in qmap.items()}
                for fp, qmap in self.mapping.items()}


def build_groups(templates: dict[str, Template],
                 max_questions: int = DEFAULT_MAX_QUESTIONS) -> list[UnionGroup]:
    """把 {fp: 模板} 编排成若干次调用；payload 相同的问题同组内只问一次。

    分片仅在超限时发生（罕见），此时同一提问可能出现在两个分片中。
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
    """按并集分组判定一条消息；分片失败不拖垮其余分片。"""
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
    fresh: int = 0        # 本次新判定的消息数
    cached: int = 0       # 直接命中缓存的消息数
    calls: int = 0        # 实际发起的 Jev 调用数
    failed: int = 0       # 全部调用失败、无法缓存的消息数
    input_tokens: int = 0
    output_tokens: int = 0


class JudgeEngine:
    """缓存优先的联合判定入口：缺失的消息用问题并集判一次，投影后落库。"""

    def __init__(self, store: Store, jev: JevClient,
                 max_questions: int = DEFAULT_MAX_QUESTIONS):
        self._store = store
        self._jev = jev
        self._max = max_questions

    async def ensure(self, channel: str, posts: list[Post],
                     templates: dict[str, Template]) -> tuple[dict[int, dict], EnsureStats]:
        """保证 posts 均有判定缓存；返回（{post_id: payload}，统计）。"""
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
