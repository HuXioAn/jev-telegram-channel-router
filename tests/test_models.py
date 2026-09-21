"""Models and rule evaluation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tgfilter.models import Template, format_answer_value


def _tpl(questions: dict, conditions: list[dict], logic: str = "all") -> Template:
    return Template.model_validate(
        {"questions": questions, "match": {"logic": logic, "conditions": conditions}})


def test_noul_evaluate_all():
    tpl = _tpl(
        {"china": {"type": "noul", "instructions": "?"}},
        [{"question": "china", "op": ">=", "value": 0.7}])
    assert tpl.evaluate({"china": {"type": "noul", "noul": 0.95}}) is True
    assert tpl.evaluate({"china": {"type": "noul", "noul": 0.3}}) is False


def test_score_and_multi_condition_all():
    tpl = _tpl(
        {"china": {"type": "noul", "instructions": "?"},
         "imp": {"type": "score", "instructions": "?", "criteria": ["a", "b", "c", "d"]}},
        [{"question": "china", "op": ">=", "value": 0.7},
         {"question": "imp", "op": ">=", "value": 1.8}])
    assert tpl.evaluate({"china": {"type": "noul", "noul": 0.9},
                         "imp": {"type": "score", "score": 2.4}}) is True
    assert tpl.evaluate({"china": {"type": "noul", "noul": 0.9},
                         "imp": {"type": "score", "score": 1.0}}) is False


def test_any_logic():
    tpl = _tpl(
        {"a": {"type": "noul", "instructions": "?"},
         "b": {"type": "noul", "instructions": "?"}},
        [{"question": "a", "op": ">=", "value": 0.5},
         {"question": "b", "op": ">=", "value": 0.5}], logic="any")
    assert tpl.evaluate({"a": {"type": "noul", "noul": 0.9},
                         "b": {"type": "noul", "noul": 0.1}}) is True
    assert tpl.evaluate({"a": {"type": "noul", "noul": 0.1},
                         "b": {"type": "noul", "noul": 0.1}}) is False


def test_choice_in_operator():
    tpl = _tpl(
        {"cat": {"type": "choice", "instructions": "?", "criteria": {"宏观": "x", "公司": "y"}}},
        [{"question": "cat", "op": "in", "value": ["宏观", "公司"]}])
    assert tpl.evaluate({"cat": {"type": "choice", "choice": "宏观"}}) is True
    assert tpl.evaluate({"cat": {"type": "choice", "choice": "其他"}}) is False


def test_missing_or_none_answer_is_false():
    tpl = _tpl({"china": {"type": "noul", "instructions": "?"}},
               [{"question": "china", "op": ">=", "value": 0.7}])
    assert tpl.evaluate({}) is False
    assert tpl.evaluate({"china": None}) is False
    assert tpl.evaluate({"china": {"type": "noul", "noul": None}}) is False


def test_structured_criteria_and_levels_accepted():
    """criteria accepts Jev's structured entries (what/examples, summary/signals)."""
    tpl = _tpl(
        {"topic": {"type": "choice", "instructions": "?",
                   "criteria": {"deals": {"what": "M&A", "examples": ["tender offer"]},
                                "none": None}},
         "severity": {"type": "score", "instructions": "?",
                      "criteria": [{"summary": "low", "signals": ["s1"]}, "high"]},
         "china": {"type": "noul", "instructions": "?",
                   "criteria": {"true": {"what": "yes"}, "false": "no"}}},
        [{"question": "china", "op": ">=", "value": 0.7}])
    assert tpl.questions["topic"].criteria["deals"]["what"] == "M&A"
    assert tpl.jev_questions()["severity"]["criteria"][0]["summary"] == "low"


def test_condition_referencing_unknown_question_rejected():
    with pytest.raises(ValidationError):
        _tpl({"china": {"type": "noul", "instructions": "?"}},
             [{"question": "missing", "op": ">=", "value": 0.5}])


def test_jev_questions_strips_title_and_none():
    tpl = _tpl({"china": {"type": "noul", "title": "相关", "instructions": "?"}},
               [{"question": "china", "op": ">=", "value": 0.7}])
    questions = tpl.jev_questions()
    assert "title" not in questions["china"]
    assert "criteria" not in questions["china"]  # None is stripped
    assert questions["china"]["type"] == "noul"


def test_invalid_question_type_rejected():
    with pytest.raises(ValidationError):
        Template.model_validate({
            "questions": {"x": {"type": "bogus", "instructions": "?"}},
            "match": {"conditions": [{"question": "x"}]}})


def test_roundtrip_json(template):
    again = Template.model_validate_json(template.model_dump_json())
    assert again == template


def test_format_answer_value():
    noul = {"type": "noul", "noul": 0.9567}
    score = {"type": "score", "score": 2.4}
    choice = {"type": "choice", "choice": "宏观"}
    class N:  # noqa: N801
        type = "noul"
    class S:  # noqa: N801
        type = "score"
    class C:  # noqa: N801
        type = "choice"
    assert format_answer_value(N, noul) == "0.96"
    assert format_answer_value(S, score) == "2.4"
    assert format_answer_value(C, choice) == "宏观"
    assert format_answer_value(N, None) == "—"
