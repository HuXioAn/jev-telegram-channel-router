"""领域模型：Post、Jev 模板（Question / Condition / Template）与规则求值。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


class Post(BaseModel):
    """频道里的一条帖子。"""

    id: int
    date: datetime | None = None
    text: str = ""
    url: str = ""


# criteria 的元素：字符串或结构化对象/数组（对齐 Jev 的 EntryType，见官方 Advanced 文档）。
# 结构化形式（如 {"what": ..., "examples": [...]}）能更精确地划定选项/等级边界。
Entry = str | dict[str, Any] | list[Any] | None


class _QuestionBase(BaseModel):
    title: str = ""  # 展示用短标签（不发给 Jev）
    instructions: str | dict | list


class NoulQuestion(_QuestionBase):
    """是/否问题，Jev 返回 0~1 概率。"""

    type: Literal["noul"] = "noul"
    criteria: dict[str, Entry] | None = None


class ChoiceQuestion(_QuestionBase):
    """从互斥选项中选择。"""

    type: Literal["choice"] = "choice"
    criteria: dict[str, Entry]


class ScoreQuestion(_QuestionBase):
    """沿有序等级评分，返回 0 起始的加权位置。"""

    type: Literal["score"] = "score"
    criteria: list[Entry] = Field(min_length=2, max_length=10)


Question = Annotated[
    Union[NoulQuestion, ChoiceQuestion, ScoreQuestion], Field(discriminator="type")
]


class Condition(BaseModel):
    """一条命中判定：question 的答案（数值/字符串）与 value 比较。"""

    question: str
    op: Literal[">=", "<=", "==", "in", "not_in"] = ">="
    value: float | str | list[str] = 0.5


class MatchSpec(BaseModel):
    logic: Literal["all", "any"] = "all"
    conditions: list[Condition]


class Template(BaseModel):
    """完整的筛选模板：问题集 + 命中规则。可反复使用。"""

    name: str = ""
    questions: dict[str, Question]
    match: MatchSpec

    @model_validator(mode="after")
    def _conditions_reference_known_questions(self) -> "Template":
        unknown = sorted({c.question for c in self.match.conditions
                          if c.question not in self.questions})
        if unknown:
            raise ValueError(
                f"match condition references unknown question id(s): {unknown}")
        return self

    def jev_questions(self) -> dict[str, dict]:
        """转成 TypeSafe systemone 接口的 questions 字段（去掉展示用 title）。"""
        return {
            qid: q.model_dump(exclude={"title"}, exclude_none=True)
            for qid, q in self.questions.items()
        }

    def evaluate(self, answers: dict[str, Any]) -> bool:
        """按 match 规则判定 answers 是否命中。"""
        checks = [
            _check(_extract_value(answers.get(c.question)), c.op, c.value)
            for c in self.match.conditions
        ]
        return all(checks) if self.match.logic == "all" else any(checks)


def _extract_value(answer: dict[str, Any] | None) -> Any:
    if not answer:
        return None
    answer_type = answer.get("type")
    if answer_type == "noul":
        return answer.get("noul")
    if answer_type == "score":
        return answer.get("score")
    if answer_type == "choice":
        return answer.get("choice")
    return None


def _check(value: Any, op: str, target: Any) -> bool:
    if value is None:
        return False
    try:
        if op == ">=":
            return float(value) >= float(target)
        if op == "<=":
            return float(value) <= float(target)
        if op == "==":
            if isinstance(target, (int, float)) and not isinstance(target, bool):
                return float(value) == float(target)
            return str(value) == str(target)
        if op == "in":
            return value in (target if isinstance(target, list) else [target])
        if op == "not_in":
            return value not in (target if isinstance(target, list) else [target])
    except (TypeError, ValueError):
        return False
    return False


def format_answer_value(question: Question, answer: dict[str, Any] | None) -> str:
    """把一条答案渲染成推送行里的短展示（0.97 / 1.8 / 选项名）。"""
    value = _extract_value(answer)
    if value is None:
        return "—"
    if question.type == "noul":
        return f"{float(value):.2f}"
    if question.type == "score":
        return f"{float(value):.1f}"
    return str(value)
