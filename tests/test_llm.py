"""模板编译器：正常路径、围栏剥离、JSON mode 降级、修复重试、失败抛错。"""
from __future__ import annotations

import json

import httpx
import pytest

from tgfilter.config import Settings
from tgfilter.llm import LLMError, TemplateCompiler

GOOD_TEMPLATE = {
    "name": "中国",
    "questions": {
        "china": {"type": "noul", "title": "相关",
                  "instructions": "是否与中国相关？",
                  "criteria": {"true": "涉及", "false": "不涉及"}}},
    "match": {"logic": "all",
              "conditions": [{"question": "china", "op": ">=", "value": 0.7}]},
}


def _settings() -> Settings:
    return Settings(openai_api_key="test-key",
                    openai_base_url="https://llm.test/v1",
                    openai_model="test-model")


def _ok_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}]})


def _compiler(handler) -> tuple[TemplateCompiler, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TemplateCompiler(http, _settings()), http


async def test_compile_success_and_sends_json_mode():
    bodies = []

    def handler(request):
        payload = json.loads(request.content)
        bodies.append(payload)
        return _ok_response(json.dumps(GOOD_TEMPLATE))

    compiler, http = _compiler(handler)
    try:
        template = await compiler.compile("中国相关的消息")
    finally:
        await http.aclose()
    assert template.name == "中国"
    assert len(bodies) == 1
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert bodies[0]["messages"][0]["role"] == "system"
    assert "中国相关的消息" in bodies[0]["messages"][1]["content"]


async def test_compile_strips_markdown_fences():
    def handler(request):
        return _ok_response("```json\n" + json.dumps(GOOD_TEMPLATE) + "\n```")

    compiler, http = _compiler(handler)
    try:
        template = await compiler.compile("中国相关的消息")
    finally:
        await http.aclose()
    assert template.name == "中国"


async def test_compile_degrades_without_json_mode():
    bodies = []

    def handler(request):
        payload = json.loads(request.content)
        bodies.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        return _ok_response(json.dumps(GOOD_TEMPLATE))

    compiler, http = _compiler(handler)
    try:
        template = await compiler.compile("中国相关的消息")
    finally:
        await http.aclose()
    assert template.name == "中国"
    assert len(bodies) == 2
    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]


async def test_compile_retries_with_error_note_on_invalid_json():
    bodies = []

    def handler(request):
        payload = json.loads(request.content)
        bodies.append(payload)
        if len(bodies) == 1:
            return _ok_response("这不是 JSON")
        return _ok_response(json.dumps(GOOD_TEMPLATE))

    compiler, http = _compiler(handler)
    try:
        template = await compiler.compile("中国相关的消息")
    finally:
        await http.aclose()
    assert template.name == "中国"
    assert len(bodies) == 2
    assert "corrected" in bodies[1]["messages"][-1]["content"]


async def test_compile_raises_after_two_failures():
    def handler(request):
        return _ok_response("依然不是 JSON")

    compiler, http = _compiler(handler)
    with pytest.raises(LLMError):
        await compiler.compile("中国相关的消息")
    await http.aclose()


async def test_compile_rejects_schema_violations():
    def handler(request):
        bad = {"questions": {"x": {"type": "bogus", "instructions": "?"}},
               "match": {"conditions": [{"question": "x"}]}}
        return _ok_response(json.dumps(bad))

    compiler, http = _compiler(handler)
    with pytest.raises(LLMError):
        await compiler.compile("x")
    await http.aclose()


async def test_compile_with_feedback_includes_previous_template():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok_response(json.dumps(GOOD_TEMPLATE))

    compiler, http = _compiler(handler)
    from conftest import make_template
    try:
        await compiler.compile("中国相关的消息", feedback="门槛提高到 0.9",
                               previous=make_template())
    finally:
        await http.aclose()
    user_message = bodies[0]["messages"][1]["content"]
    assert "Requested adjustment" in user_message
    assert "门槛提高到 0.9" in user_message
    assert "china" in user_message  # 上一版模板被带上


async def test_system_prompt_mandates_english_output_and_covers_jev_rules():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok_response(json.dumps(GOOD_TEMPLATE))

    compiler, http = _compiler(handler)
    try:
        await compiler.compile("描述")
    finally:
        await http.aclose()
    system = bodies[0]["messages"][0]["content"]
    assert "ENGLISH" in system  # 模板内容一律英文
    for token in ("noul", "choice", "score", "not_in", "score:", "snap judgment"):
        assert token in system, token


async def test_compile_degrades_when_temperature_rejected():
    """部分推理模型拒绝 temperature：应再降一级，且不改 JSON 解析结果。"""
    bodies = []

    def handler(request):
        payload = json.loads(request.content)
        bodies.append(payload)
        if "temperature" in payload:
            return httpx.Response(400, json={"error": "temperature is not supported"})
        return _ok_response(json.dumps(GOOD_TEMPLATE))

    compiler, http = _compiler(handler)
    try:
        template = await compiler.compile("中国相关的消息")
    finally:
        await http.aclose()
    assert template.name == "中国"
    assert len(bodies) == 3
    assert "response_format" in bodies[0] and "temperature" in bodies[0]
    assert "response_format" not in bodies[1] and "temperature" in bodies[1]
    assert "response_format" not in bodies[2] and "temperature" not in bodies[2]
