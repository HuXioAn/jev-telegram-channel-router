"""Template compiler: natural-language description -> Jev template (single-turn LLM call, structured JSON output).

Design requirements (see PLAN.md §8): no agent loop; a failed validation triggers only one automatic repair retry;
compatible with any OpenAI-compatible endpoint (endpoints without JSON mode support degrade automatically).

The Jev knowledge in the prompt is distilled from the official docs (docs.typesafe.ai, reviewed 2026-09):
the primitives / noul / choice / score / advanced(structure) / state pages.
Key points: English is the main training language; questions are independent and judged at a single point; noul is a probability, not a degree;
score levels must describe concrete situations and be evaluated independently; criteria supports structured objects.
"""
from __future__ import annotations

import json
import re

import httpx

from .config import Settings
from .models import Template

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class LLMError(Exception):
    def __init__(self, message: str, usage: dict | None = None):
        super().__init__(message)
        self.usage: dict = usage or {}  # real token usage generated before the failure


_PROMPT_HEAD = """You are the Filter Template Compiler. You convert a user's plain-language description of what they want to catch from Telegram channels into ONE strict JSON document: a reusable "Jev template" that a judgment model (Jev, by TypeSafe) executes against every incoming message.

Output ONLY the JSON document. No prose, no explanations, no markdown fences.

# How Jev works
- Jev answers typed questions about ONE piece of text. Here the state is the raw text of a single Telegram message. It returns calibrated numeric judgments, never prose.
- All questions in the template run in parallel and independently over the same message. A question cannot see another question's answer, so each question must be fully self-contained — never refer to other question ids.
- One snap judgment per question: something a knowledgeable reader decides at a glance. Never combine two conditions into one question; never ask for multi-step analysis.
- Jev is trained primarily on English and judges English instructions most accurately. ALWAYS write every template field (name, title, instructions, criteria) in ENGLISH, even when the user writes in Chinese or another language. The messages being judged may be in any language; English instructions handle them fine.
- Use 1-4 questions. Split independent dimensions (e.g. "is it about China" vs "how important is it") and let the match rule combine them.

# Question types
- "noul": a YES/NO question; the answer is P(yes) from 0 to 1.
  Use it for clean yes/no properties ("Is this message directly about China?").
  Phrase it so a HIGH value means YES; never invert.
  0.5 means "yes and no equally likely"; it is NOT a medium degree. If the user describes a degree (how important / how severe / how big), use "score" instead.
  When the yes/no boundary is subtle, add criteria {"true": ..., "false": ...} describing what counts as each side. A side may be a string or a structured object like {"what": "...", "examples": ["..."], "not_for": "..."}.
- "choice": pick exactly ONE of a fixed, mutually exclusive list; the answer is the chosen option key.
  criteria is an object {"option_key": "description", ...}. Keys are short English snake_case.
  Options must not overlap; add an "other" option when the list might not cover everything.
  Option descriptions may be structured objects like {"what": "...", "not_for": "...", "examples": ["..."]}.
- "score": a position along ORDERED levels (low to high); the answer is a probability-weighted position that may fall between levels.
  criteria is an ORDERED ARRAY of 2-10 levels, from low to high.
  Each level is judged on its own, so describe a CONCRETE situation, never a bare number or a vague degree. "Moderately important" is bad; "affects one company; routine disclosure" is good.
  Keep one dimension per score question. 3-5 well-separated levels are usually best.
  Levels may be structured objects like {"summary": "...", "signals": ["..."]} when extra precision helps.

# Template fields
- "name": short English template name, at most 40 characters.
- "questions": a map from id to question. Ids are short English snake_case, e.g. "china_relevance". Ids are for code only; write the complete meaning inside instructions.
- "title": per-question English display label for the chat UI, at most 30 characters, e.g. "China-related".
- "instructions": the self-contained question about "the message". Define ambiguous terms inline; add exclusions when confusion is plausible. A structured object like {"question": "...", "focus": "...", "note": "..."} is allowed when it adds clarity.

# Match rule (what counts as a hit)
- "match.logic": "all" (AND, default) or "any" (OR).
- "match.conditions": a list of {"question": "<id>", "op": ">=", "value": ...}. Allowed ops: ">=", "<=", "==", "in", "not_in".
- noul: use ">=" with a threshold. 0.7-0.9 means a strong yes; raise it when a false positive is expensive, lower toward 0.6 when recall matters more.
- score: the threshold is a level position. On a 4-level scale (0..3), ">= 2" keeps the top 2 levels; pick the levels the user actually wants.
- choice: "in" with the list of accepted option keys.
- Every condition's "question" MUST be one of the question ids.
- Default to "all" when the user describes several conditions that must hold; use "any" (or a single "in" condition) for unions of alternatives."""

_SCHEMA_SPEC = """# JSON schema (strict)
{
  "name": "<short English name>",
  "questions": {
    "<question_id>": {
      "type": "noul | choice | score",
      "title": "<short English UI label>",
      "instructions": "<English, self-contained, about 'the message'>",
      "criteria": "noul: {\\"true\\": <desc>, \\"false\\": <desc>} (optional) | choice: {\\"option_key\\": <desc>, ...} | score: [<level 0>, <level 1>, ...]  —  each <desc> / <level> is a string OR a structured object"
    }
  },
  "match": {
    "logic": "all | any",
    "conditions": [{"question": "<id>", "op": ">= | <= | == | in | not_in", "value": <number | string | list of strings>}]
  }
}"""

_PROMPT_EXAMPLES = """# Examples

User description (Chinese): "中国相关的重磅财经消息，重要度高的"
Output:
{
  "name": "China Finance Watch",
  "questions": {
    "china_relevance": {
      "type": "noul",
      "title": "China-related",
      "instructions": "Is this message directly about China (including Hong Kong, Macau and Taiwan) — its markets, economy, policies, regulators, companies, industries or assets?",
      "criteria": {
        "true": {"what": "Concerns China's market, economy, policy, a Chinese company, industry, or a China-linked asset", "examples": ["PBOC policy moves", "A-share or HK-listed company news"]},
        "false": {"what": "Purely foreign content with no direct China link", "not_for": "Global stories that merely mention China in passing"}
      }
    },
    "finance_relevance": {
      "type": "noul",
      "title": "Finance",
      "instructions": "Is this message about finance or business — markets, macroeconomics, monetary or regulatory policy, corporate finance, deals, earnings, or other material business developments?",
      "criteria": {
        "true": {"what": "Belongs to the finance / business domain"},
        "false": {"what": "General news, tech, lifestyle, sports or entertainment without financial substance"}
      }
    },
    "importance": {
      "type": "score",
      "title": "Importance",
      "instructions": "How important is this message for someone actively following Chinese financial markets? Judge the potential market impact and the scale or prominence of what is affected.",
      "criteria": [
        {"summary": "Routine or minor: generic commentary, low-stakes updates, no visible market impact", "signals": ["Daily commentary", "Small routine disclosures"]},
        {"summary": "Notable: meaningful single-company or sector news worth knowing", "signals": ["Notable earnings or contracts", "Sector-level regulatory tweaks"]},
        {"summary": "Significant: large-scale moves, important policy signals, major deals", "signals": ["Major policy shifts", "Multi-billion-dollar deals", "Index-level events"]},
        {"summary": "Major: market-moving, systemic or high-impact breaking news", "signals": ["Central bank rate moves", "Market-wide interventions", "Crisis or rescue events"]}
      ]
    }
  },
  "match": {
    "logic": "all",
    "conditions": [
      {"question": "china_relevance", "op": ">=", "value": 0.7},
      {"question": "finance_relevance", "op": ">=", "value": 0.7},
      {"question": "importance", "op": ">=", "value": 2.0}
    ]
  }
}

User description (Chinese): "只要并购或 IPO 相关的消息"
Output:
{
  "name": "M&A and IPO Watch",
  "questions": {
    "deal_type": {
      "type": "choice",
      "title": "Deal type",
      "instructions": "What type of corporate deal does this message primarily concern?",
      "criteria": {
        "merger_acquisition": "Mergers, acquisitions, takeovers, stake purchases or tender offers",
        "ipo_listing": "IPOs, new listings, spin-offs or going public",
        "none": "No corporate deal of these kinds is the main subject"
      }
    }
  },
  "match": {
    "logic": "all",
    "conditions": [{"question": "deal_type", "op": "in", "value": ["merger_acquisition", "ipo_listing"]}]
  }
}

Now produce the template for the user's description below. Output ONLY the JSON document."""

_SYSTEM_PROMPT = "\n\n".join([_PROMPT_HEAD, _SCHEMA_SPEC, _PROMPT_EXAMPLES])


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


class TemplateCompiler:
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self._http = http
        self._api_key = settings.openai_api_key
        self._base = settings.openai_base_url.rstrip("/")
        self._model = settings.openai_model
        self._timeout = 90.0

    async def compile(self, description: str, feedback: str | None = None,
                      previous: Template | None = None) -> tuple[Template, dict]:
        """Description (optionally with adjustment feedback and the previous template) -> (Template, usage).

        usage = {"input_tokens", "output_tokens", "calls"} holds the real usage
        returned by the API (accumulated across automatic repair retries); on failure raises LLMError (which also carries .usage).
        """
        user_parts = [f"User description: {description.strip()}"]
        if previous is not None and feedback:
            user_parts.append(f"Previous template:\n{previous.model_dump_json()}")
            user_parts.append(f"Requested adjustment: {feedback.strip()}")
        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

        last_error: str | None = None
        totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        for _ in range(2):  # original attempt + one repair retry
            attempt_messages = list(messages)
            if last_error:
                attempt_messages.append({
                    "role": "user",
                    "content": (f"Your previous output was rejected ({last_error}). "
                                "Output ONLY the corrected JSON document."),
                })
            raw, tokens = await self._chat(attempt_messages)
            totals["input_tokens"] += tokens["input_tokens"]
            totals["output_tokens"] += tokens["output_tokens"]
            totals["calls"] += 1
            try:
                data = json.loads(_strip_fences(raw))
                return Template.model_validate(data), totals
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)[:500]
        raise LLMError(f"模板编译失败：{last_error}", usage=totals)

    async def _chat(self, messages: list[dict]) -> tuple[str, dict]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        base: dict = {"model": self._model, "messages": messages}
        # Graceful degradation to fit the various OpenAI-compatible endpoints:
        #   1) json_mode + temperature=0 (most stable output)
        #   2) drop response_format (endpoint does not support JSON mode)
        #   3) drop temperature as well (some reasoning models only accept the default temperature)
        variants: list[dict] = [
            {**base, "temperature": 0, "response_format": {"type": "json_object"}},
            {**base, "temperature": 0},
            {**base},
        ]
        last_error = "unknown"
        for index, payload in enumerate(variants):
            response = await self._http.post(
                f"{self._base}/chat/completions", json=payload, headers=headers,
                timeout=self._timeout)
            if response.status_code == 400 and index < len(variants) - 1:
                last_error = f"HTTP 400: {response.text[:200]}"
                continue
            if response.status_code != 200:
                raise LLMError(f"LLM HTTP {response.status_code}: {response.text[:200]}")
            try:
                payload = response.json()
                content = payload["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"LLM 响应结构异常：{exc}") from exc
            usage = payload.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            return content, {
                "input_tokens": int(usage.get("prompt_tokens")
                                    or usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens")
                                     or usage.get("output_tokens") or 0),
            }
        raise LLMError(f"LLM 调用失败（参数降级后仍被拒：{last_error}）")
