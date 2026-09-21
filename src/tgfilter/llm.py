"""模板编译器：自然语言描述 → Jev 模板（单轮 LLM 调用，结构化 JSON 输出）。

设计要求（见 PLAN.md §8）：无 agent loop；校验失败仅自动修复重试一次；
兼容任意 OpenAI 兼容端点（不支持 JSON mode 的端点自动降级）。
"""
from __future__ import annotations

import json
import re

import httpx

from .config import Settings
from .models import Template

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class LLMError(Exception):
    pass


_SCHEMA_SPEC = """{
  "name": "模板短名（≤12字）",
  "questions": {
    "<问题id：英文小写>": {
      "type": "noul 或 choice 或 score",
      "title": "≤6字中文短标签（用于展示）",
      "instructions": "自包含的问题描述（判据写清楚）",
      "criteria": "noul: {\\"true\\": \\"是的情形\\", \\"false\\": \\"否的情形\\"} | choice: {\\"选项A\\": \\"描述\\", ...} | score: [\\"等级0描述\\", \\"等级1描述\\", ...]"
    }
  },
  "match": {
    "logic": "all 或 any",
    "conditions": [{"question": "问题id", "op": ">= 或 <= 或 == 或 in 或 not_in", "value": "阈值"}]
  }
}"""

_SYSTEM_PROMPT = f"""你是「过滤器模板编译器」：把用户想从 Telegram 频道筛选内容的自然语言描述，编译成一个可反复执行的 Jev 判定模板，只输出 JSON。

Jev 是判定模型：对它提问，它返回类型化答案（不是自由文本）。可用问题类型：
- noul：是/否问题，返回 0~1 概率。
- choice：从给定互斥选项中选一个。
- score：沿有序等级评分，返回 0 起始的等级加权位置（如 4 个等级取值 0~3）。

设计要求：
1. 模板必须自包含：Jev 没有外部上下文，问题和判据里要写清楚定义（例如把「与中国相关」定义为「涉及中国的市场/政策/公司/行业等」）。
2. 每个问题单一明确；选项/等级相互独立、判据具体，必要时给例子。
3. 问题数量 1~4 个即可，不要冗余。
4. 问题与判据的语言跟随用户描述的语言。
5. match.conditions 用阈值定义「命中」：noul 常用 >= 0.6~0.8；score 阈值是等级位置；choice 用 in。
6. 只输出 JSON，不要解释、不要 markdown 代码块。

JSON schema（严格遵循）：
{_SCHEMA_SPEC}

示例：用户说「中国相关的重磅消息」→
{{"name":"中国重磅","questions":{{"china":{{"type":"noul","title":"相关","instructions":"这条消息是否与中国（含港澳台）的市场、政策、公司、行业直接相关？","criteria":{{"true":"内容涉及中国市场/政策/公司/行业等","false":"纯海外内容，不涉及中国"}}}},"importance":{{"type":"score","title":"重要","instructions":"这条消息对关注市场的人有多重要？","criteria":["日常资讯","一般","较高","重大"]}}}},"match":{{"logic":"all","conditions":[{{"question":"china","op":">=","value":0.7}},{{"question":"importance","op":">=","value":1.8}}]}}}}"""


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
                      previous: Template | None = None) -> Template:
        """描述（可带调整意见与上一版模板）→ Template。失败抛 LLMError。"""
        user_parts = [f"用户描述：{description.strip()}"]
        if previous is not None and feedback:
            user_parts.append(f"上一版模板：{previous.model_dump_json()}")
            user_parts.append(f"用户的调整意见：{feedback.strip()}")
        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

        last_error: str | None = None
        for _ in range(2):  # 原始尝试 + 一次修复重试
            attempt_messages = list(messages)
            if last_error:
                attempt_messages.append({
                    "role": "user",
                    "content": f"上次输出不符合要求（错误：{last_error}）。请仅输出修正后的 JSON。",
                })
            raw = await self._chat(attempt_messages)
            try:
                data = json.loads(_strip_fences(raw))
                return Template.model_validate(data)
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)[:500]
        raise LLMError(f"模板编译失败：{last_error}")

    async def _chat(self, messages: list[dict]) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        base: dict = {"model": self._model, "messages": messages}
        # 逐级降级以适配各家 OpenAI 兼容端点：
        #   ① json_mode + temperature=0（输出最稳）
        #   ② 去掉 response_format（端点不支持 JSON mode）
        #   ③ 再去掉 temperature（部分推理模型只接受默认温度）
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
                return response.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"LLM 响应结构异常：{exc}") from exc
        raise LLMError(f"LLM 调用失败（参数降级后仍被拒：{last_error}）")
