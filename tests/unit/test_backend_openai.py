from __future__ import annotations

import json

import httpx
import pytest

from poseidon.ai.backends.base import ToolResult
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.core.config import AIConfig
from poseidon.core.errors import AgentError, BackendUnreachableError


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
    # LM Studio only accepts string tool_choice; "required" forces the sole tool.
    assert captured["body"]["tool_choice"] == "required"


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

    # A 5xx means the server is up but erroring — a plain AgentError, NOT the
    # "unreachable" subtype.
    with pytest.raises(AgentError) as exc_info:
        await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert not isinstance(exc_info.value, BackendUnreachableError)


async def test_connect_error_becomes_backend_unreachable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    with pytest.raises(BackendUnreachableError):
        await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")


async def test_non_object_json_is_plain_agenterror() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])  # valid JSON, not an object

    with pytest.raises(AgentError) as exc_info:
        await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert not isinstance(exc_info.value, BackendUnreachableError)


def test_tool_result_messages_one_per_call() -> None:
    b = OpenAICompatibleBackend(_cfg(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})))
    msgs = b.tool_result_messages([ToolResult("c1", "quote json"), ToolResult("c2", "err", True)])
    assert [m["role"] for m in msgs] == ["tool", "tool"]
    assert msgs[0]["tool_call_id"] == "c1"


async def _error_message_for(body_json: dict | None = None, *, status: int = 400,
                             text: str | None = None) -> str:
    def handler(req: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body_json)

    with pytest.raises(AgentError) as exc_info:
        await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert not isinstance(exc_info.value, BackendUnreachableError)  # server is up
    return str(exc_info.value)


async def test_http_400_surfaces_server_body_without_hint() -> None:
    # The response body is the only place the server says WHY it rejected the
    # request; the component-error notification is built from this message.
    # A non-overflow rejection must NOT carry the context-length remedy.
    msg = await _error_message_for({"error": "invalid 'tool_choice' value"})
    assert "invalid 'tool_choice' value" in msg
    assert "--context-length" not in msg


async def test_context_overflow_400_names_the_fix() -> None:
    # Exact body shape LM Studio returns when the prompt exceeds the loaded
    # context window (the JSON detail arrives embedded in the error STRING).
    body = {"error": "Engine protocol predict request returned 400: {\"error\":{\"code\":400,"
                     "\"message\":\"request (12073 tokens) exceeds the available context size "
                     "(8192 tokens), try increasing it\",\"type\":\"exceed_context_size_error\","
                     "\"n_prompt_tokens\":12073,\"n_ctx\":8192}}"}
    msg = await _error_message_for(body)
    assert "exceeds the available context size" in msg  # server's own diagnosis surfaced
    # Remedy names the CONFIGURED model (copy-pasteable), with no angle-bracket
    # placeholder: KDE's body-markup notification daemon strips unknown <tags>.
    assert "lms load devstral --context-length 32768" in msg
    assert "<model>" not in msg  # placeholder would be stripped as markup by the daemon


@pytest.mark.parametrize("body", [
    # llama.cpp native object-valued error (not string-embedded like LM Studio)
    {"error": {"code": 400, "message": "the request exceeds the available context size, "
                                       "try increasing it", "type": "exceed_context_size_error"}},
    # vLLM / OpenAI-platform message phrasing
    {"error": {"message": "This model's maximum context length is 8192 tokens. However, you "
                          "requested 12073 tokens. Please reduce the length of the messages.",
               "type": "invalid_request_error", "code": "context_length_exceeded"}},
    # Anthropic-style phrasing some proxies relay
    {"error": {"message": "prompt is too long: 200000 tokens > 100000 maximum"}},
])
async def test_overflow_hint_fires_across_vendor_phrasings(body: dict) -> None:
    msg = await _error_message_for(body)
    assert "--context-length" in msg


async def test_hint_not_baited_by_incidental_context_words() -> None:
    # A traceback body mentioning contextlib + "exceeded" must not trigger the
    # context-window remedy — a bogus remediation on an unrelated incident
    # actively misleads the operator.
    bait = ("Internal error: Traceback (most recent call last): File "
            "\"/usr/lib/python3.11/contextlib.py\", line 81, in inner "
            "RecursionError: maximum recursion depth exceeded")
    msg = await _error_message_for(status=500, text=bait, body_json=None)
    assert "contextlib.py" in msg  # body still surfaced
    assert "--context-length" not in msg  # no bogus remedy


async def test_http_error_body_is_bounded_single_line_and_truncated_not_dropped() -> None:
    # A pathological body must not balloon the error message (it feeds desktop
    # notifications and structlog lines), must collapse to ONE line, and must
    # be truncated — not dropped — so the leading diagnosis survives.
    msg = await _error_message_for(text="AAAA\n" + "C" * 9_000, body_json=None)
    assert len(msg) < 700
    assert "\n" not in msg
    assert "AAAA" in msg


async def test_http_error_empty_body_still_names_status() -> None:
    msg = await _error_message_for(status=502, text="", body_json=None)
    assert "502" in msg
