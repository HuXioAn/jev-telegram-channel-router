"""联合判定引擎：指纹、问题并集（去重/分片）、缓存与投影。"""
from __future__ import annotations

from conftest import make_template
from tgfilter.judging import (JudgeEngine, build_groups, template_fingerprint)
from tgfilter.models import Post, Template
from tgfilter.store import Store

CHINA = "是否与中国相关？"


def multi_template(n: int, *, prefix: str = "q") -> Template:
    """构造含 n 个问题的模板（问题 i 的 instructions 各不相同）。"""
    questions = {f"{prefix}{i}": {"type": "noul", "title": f"{prefix}{i}",
                                  "instructions": f"问题 {prefix}{i}？"}
                 for i in range(n)}
    return Template.model_validate({
        "name": f"{prefix}-{n}",
        "questions": questions,
        "match": {"logic": "all",
                  "conditions": [{"question": f"{prefix}0", "op": ">=", "value": 0.5}]},
    })


class EchoJev:
    """对每个 asked 问题回同一个分数；可指定整体失败的消息。"""

    def __init__(self, score: float = 0.9, fail_texts: set[str] | None = None):
        self.score = score
        self.fail_texts = fail_texts or set()
        self.calls: list[tuple[str, int]] = []

    async def classify_questions(self, text: str, questions: dict) -> dict:
        self.calls.append((text, len(questions)))
        if text in self.fail_texts:
            return {"error": "boom"}
        return {"answers": {aid: {"type": "noul", "noul": self.score}
                            for aid in questions},
                "usage": {"input_tokens": 10, "output_tokens": 2}}


# ------------------------------------------------------------------ 指纹
def test_template_fingerprint_tracks_questions_only():
    """指纹只取决于发给 Jev 的问题集：改阈值（命中规则）不变；改问题描述则变。"""
    base = make_template()
    other_threshold = make_template(
        match={"logic": "all",
               "conditions": [{"question": "china", "op": ">=", "value": 0.5}]})
    other_question = make_template(
        questions={"china": {"type": "noul", "title": "相关",
                             "instructions": "换个问法？"}})
    assert template_fingerprint(base) == template_fingerprint(make_template())
    assert template_fingerprint(base) == template_fingerprint(other_threshold)
    assert template_fingerprint(base) != template_fingerprint(other_question)


# ------------------------------------------------------------------ 编排
def test_build_groups_dedupes_identical_questions():
    """问题 payload 相同（跨模板）→ 并集里只问一次，映射分别指回各模板。"""
    tpl_a = make_template()
    tpl_b = make_template(match={"logic": "all",
                                 "conditions": [{"question": "china",
                                                 "op": ">=", "value": 0.5}]})
    fp_a, fp_b = template_fingerprint(tpl_a), template_fingerprint(tpl_b)
    groups = build_groups({fp_a: tpl_a, fp_b: tpl_b})

    assert len(groups) == 1
    group = groups[0]
    assert len(group.asked) == 1
    asked_id = next(iter(group.asked))
    assert group.mapping[fp_a]["china"] == asked_id
    assert group.mapping[fp_b]["china"] == asked_id


def test_build_groups_splits_over_cap():
    """超过单次问题上限：按模板分片（单个模板超限时独占一片）。"""
    tpl_big = multi_template(3, prefix="a")
    tpl_small = multi_template(1, prefix="b")
    groups = build_groups({template_fingerprint(tpl_big): tpl_big,
                           template_fingerprint(tpl_small): tpl_small},
                          max_questions=2)

    assert [len(group.asked) for group in groups] == [3, 1]
    assert set(groups[0].mapping) == {template_fingerprint(tpl_big)}
    assert set(groups[1].mapping) == {template_fingerprint(tpl_small)}


def test_project_maps_answers_back_to_local_questions():
    tpl = make_template()
    fp = template_fingerprint(tpl)
    group = build_groups({fp: tpl})[0]
    asked_id = next(iter(group.asked))
    answer = {"type": "noul", "noul": 0.77}
    projected = group.project({asked_id: answer})
    assert projected == {fp: {"china": answer}}


# ------------------------------------------------------------------ 引擎
async def test_judge_engine_caches_and_counts(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    jev = EchoJev()
    engine = JudgeEngine(store, jev)
    tpl = make_template()
    fp = template_fingerprint(tpl)
    posts = [Post(id=1, text="a", url="u1"), Post(id=2, text="b", url="u2")]

    cache, stats = await engine.ensure("chan", posts, {fp: tpl})

    assert stats.fresh == 2 and stats.cached == 0 and stats.calls == 2
    assert stats.failed == 0
    assert set(cache) == {1, 2}
    assert store.judgments_for("chan", [1])[1] == {
        fp: {"china": {"type": "noul", "noul": 0.9}}}

    calls_before = list(jev.calls)
    cache2, stats2 = await engine.ensure("chan", posts, {fp: tpl})
    assert stats2.cached == 2 and stats2.fresh == 0
    assert jev.calls == calls_before                     # 缓存命中 → 不再调用
    assert set(cache2) == {1, 2}


async def test_judge_engine_partial_failure_not_cached(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    engine = JudgeEngine(store, EchoJev(fail_texts={"a"}))
    tpl = make_template()
    posts = [Post(id=1, text="a", url="u1"), Post(id=2, text="b", url="u2")]

    cache, stats = await engine.ensure("chan", posts, {template_fingerprint(tpl): tpl})

    assert stats.failed == 1 and stats.calls == 2
    assert set(cache) == {2}                             # 失败的消息不落缓存
    assert store.judgments_for("chan", [1]) == {}


async def test_judge_engine_splits_over_cap(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    jev = EchoJev()
    engine = JudgeEngine(store, jev, max_questions=3)
    tpl_a, tpl_b = multi_template(2, prefix="a"), multi_template(2, prefix="b")
    fp_a, fp_b = template_fingerprint(tpl_a), template_fingerprint(tpl_b)

    cache, stats = await engine.ensure("chan", [Post(id=1, text="x", url="u")],
                                       {fp_a: tpl_a, fp_b: tpl_b})

    assert stats.calls == 2                              # 2+2 问 > 3 → 分两片
    assert jev.calls == [("x", 2), ("x", 2)]
    assert set(cache[1]) == {fp_a, fp_b}                 # 结果合并到同一条消息
    assert cache[1][fp_a] == {"a0": {"type": "noul", "noul": 0.9},
                              "a1": {"type": "noul", "noul": 0.9}}
