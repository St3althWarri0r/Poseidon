from __future__ import annotations

import json

import httpx
import pytest

from poseidon.ai.backends.base import ToolResult
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.core.config import AIConfig
from poseidon.core.errors import AgentError


def _cfg() -> AIConfig:
    return AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1",
                    model="devstral", temperature=0.2)


def _backend(handler) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(_cfg(), transport=httpx.MockTransport(handler))


async def test_tool_call_parsed_and_system_injected() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"model": "devstral", "choices": [{
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_quote", "arguments": "{\"symbol\": \"AAPL\"}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    b = _backend(handler)
    tools = [{"name": "get_quote", "description": "q", "input_schema": {
        "type": "object", "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"], "additionalProperties": False}, "strict": True}]
    resp = await b.complete([{"role": "user", "content": "hi"}], tools=tools, system="SYS")

    assert captured["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert captured["body"]["tools"][0]["type"] == "function"
    assert captured["body"]["tools"][0]["function"]["name"] == "get_quote"
    assert captured["body"]["tool_choice"] == "auto"
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].input == {"symbol": "AAPL"}
    assert resp.usage["input_tokens"] == 10 and resp.usage["output_tokens"] == 5
    assert resp.assistant_message["content"] == ""  # null normalized for re-send
    await b.aclose()


async def test_force_tool_sets_tool_choice() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"model": "d", "choices": [{
            "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "rev", "arguments": "{}"}}]}}], "usage": {}})

    b = _backend(handler)
    await b.complete([{"role": "user", "content": "x"}],
                     tools=[{"name": "rev", "description": "", "input_schema": {"type": "object"}}],
                     system="s", force_tool="rev")
    assert captured["body"]["tool_choice"] == {"type": "function", "function": {"name": "rev"}}


async def test_plain_text_end() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "devstral", "choices": [{
            "finish_reason": "stop", "message": {"role": "assistant", "content": "no action"}}],
            "usage": {}})

    resp = await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert resp.stop_reason == "end" and resp.text == "no action"


async def test_unparseable_arguments_dropped_not_fabricated() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "d", "choices": [{
            "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "get_quote", "arguments": "{bad"}}]}}], "usage": {}})

    resp = await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert resp.tool_calls == []  # dropped, never invented


async def test_http_error_becomes_agenterror() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(AgentError):
        await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")


def test_tool_result_messages_one_per_call() -> None:
    b = OpenAICompatibleBackend(_cfg(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})))
    msgs = b.tool_result_messages([ToolResult("c1", "quote json"), ToolResult("c2", "err", True)])
    assert [m["role"] for m in msgs] == ["tool", "tool"]
    assert msgs[0]["tool_call_id"] == "c1"
