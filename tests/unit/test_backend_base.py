from __future__ import annotations

from poseidon.ai.backends.base import ChatBackend, ToolCall, ToolResult

from .backend_fakes import FakeBackend, tool_use


def test_toolcall_and_result_construct() -> None:
    tc = ToolCall(id="x", name="get_quote", input={"symbol": "AAPL"})
    assert tc.input["symbol"] == "AAPL"
    assert ToolResult("x", "ok").is_error is False


async def test_fakebackend_is_a_chatbackend() -> None:
    fb = FakeBackend([tool_use(ToolCall("1", "get_quote", {"symbol": "AAPL"}))])
    assert isinstance(fb, ChatBackend)
    resp = await fb.complete([], tools=[], system="s")
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].name == "get_quote"


def test_tool_result_messages_shape() -> None:
    fb = FakeBackend([])
    msgs = fb.tool_result_messages([ToolResult("c1", "quote", False)])
    assert msgs[0]["content"][0]["tool_call_id"] == "c1"
