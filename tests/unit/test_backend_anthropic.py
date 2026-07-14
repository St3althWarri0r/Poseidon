from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from poseidon.ai.backends.anthropic_backend import AnthropicBackend
from poseidon.core.config import AIConfig


class _FakeMessages:
    def __init__(self, resp: Any, sink: dict) -> None:
        self._resp, self._sink = resp, sink

    async def create(self, **kwargs: Any) -> Any:
        self._sink.update(kwargs)
        return self._resp


class _FakeClient:
    def __init__(self, resp: Any, sink: dict) -> None:
        self.messages = _FakeMessages(resp, sink)

    async def close(self) -> None:
        return None


def _resp(stop: str, content: list, model: str = "claude-x") -> Any:
    return SimpleNamespace(stop_reason=stop, content=content, model=model,
        usage=SimpleNamespace(input_tokens=7, output_tokens=3,
            cache_read_input_tokens=0, cache_creation_input_tokens=0))


def _block(**kw: Any) -> Any:
    return SimpleNamespace(**kw)


async def test_maps_tool_use_and_keeps_thinking_without_force() -> None:
    sink: dict = {}
    resp = _resp("tool_use", [_block(type="tool_use", id="1", name="get_quote",
                                     input={"symbol": "AAPL"})])
    b = AnthropicBackend(AIConfig(), api_key="k", client=_FakeClient(resp, sink))
    out = await b.complete([{"role": "user", "content": "hi"}],
                           tools=[{"name": "get_quote"}], system="SYS")
    assert out.stop_reason == "tool_use"
    assert out.tool_calls[0].input == {"symbol": "AAPL"}
    assert out.usage["input_tokens"] == 7
    assert sink["thinking"] == {"type": "adaptive"}
    assert sink["system"][0]["text"] == "SYS"


async def test_force_tool_omits_thinking() -> None:
    sink: dict = {}
    resp = _resp("tool_use", [_block(type="tool_use", id="1", name="rev", input={})])
    b = AnthropicBackend(AIConfig(), api_key="k", client=_FakeClient(resp, sink))
    await b.complete([{"role": "user", "content": "x"}], tools=[{"name": "rev"}],
                     system="S", force_tool="rev")
    assert "thinking" not in sink  # forced tool_choice ⇒ no extended thinking
    assert sink["tool_choice"] == {"type": "tool", "name": "rev"}


async def test_refusal_and_text_mapping() -> None:
    b = AnthropicBackend(AIConfig(), api_key="k", client=_FakeClient(_resp("refusal", []), {}))
    assert (await b.complete([], tools=[], system="s")).stop_reason == "refusal"
    b2 = AnthropicBackend(AIConfig(), api_key="k",
                          client=_FakeClient(_resp("end_turn", [_block(type="text", text="hello")]), {}))
    out = await b2.complete([], tools=[], system="s")
    assert out.stop_reason == "end" and out.text == "hello"


async def test_pause_turn_maps_to_pause() -> None:
    b = AnthropicBackend(AIConfig(), api_key="k",
                         client=_FakeClient(_resp("pause_turn", []), {}))
    assert (await b.complete([], tools=[], system="s")).stop_reason == "pause"
