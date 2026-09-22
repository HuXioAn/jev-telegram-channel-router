"""Domain models: Post, Jev template (Question / Condition / Template) and rule evaluation."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


DEFAULT_MAX_POST_CHARS = 500   # post text judged by Jev / delivered (the rest lives behind the link)
TELEGRAM_TEXT_LIMIT = 4000     # hard cap for one outgoing text (Telegram allows 4096)


def clip_text(text: str, limit: int) -> str:
    """Clip text to limit characters, marking a cut with an ellipsis."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


class Post(BaseModel):
    """A single post in a channel."""

    id: int
    date: datetime | None = None
    text: str = ""
    url: str = ""
    channel: str = ""        # source channel username (link target of the footer)
    channel_title: str = ""  # display name of the source (page og:title)
    truncated: bool = False  # text was clipped (delivery marks it at the message end)


# Elements of criteria: a string or a structured object/array (mirrors Jev's EntryType, see the official Advanced docs).
# The structured form (e.g. {"what": ..., "examples": [...]}) pins down option/level boundaries more precisely.
Entry = str | dict[str, Any] | list[Any] | None


class _QuestionBase(BaseModel):
    title: str = ""  # short label for display (not sent to Jev)
    instructions: str | dict | list


class NoulQuestion(_QuestionBase):
    """Yes/no question; Jev returns a probability between 0 and 1."""

    type: Literal["noul"] = "noul"
    criteria: dict[str, Entry] | None = None


class ChoiceQuestion(_QuestionBase):
    """Choose among mutually exclusive options."""

    type: Literal["choice"] = "choice"
    criteria: dict[str, Entry]


class ScoreQuestion(_QuestionBase):
    """Score along an ordered scale; returns a weighted position starting at 0."""

    type: Literal["score"] = "score"
    criteria: list[Entry] = Field(min_length=2, max_length=10)


Question = Annotated[
    Union[NoulQuestion, ChoiceQuestion, ScoreQuestion], Field(discriminator="type")
]


class Condition(BaseModel):
    """A single match check: the answer to question (numeric/string) compared against value."""

    question: str
    op: Literal[">=", "<=", "==", "in", "not_in"] = ">="
    value: float | str | list[str] = 0.5


class MatchSpec(BaseModel):
    logic: Literal["all", "any"] = "all"
    conditions: list[Condition]


class Template(BaseModel):
    """A complete filtering template: question set + match rules. Reusable."""

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
        """Convert into the TypeSafe systemone questions field (drops the display-only title)."""
        return {
            qid: q.model_dump(exclude={"title"}, exclude_none=True)
            for qid, q in self.questions.items()
        }

    def evaluate(self, answers: dict[str, Any]) -> bool:
        """Decide whether answers match, according to the match rules."""
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
    """Render one answer as a short display value for a push line (0.97 / 1.8 / option name)."""
    value = _extract_value(answer)
    if value is None:
        return "—"
    if question.type == "noul":
        return f"{float(value):.2f}"
    if question.type == "score":
        return f"{float(value):.1f}"
    return str(value)
