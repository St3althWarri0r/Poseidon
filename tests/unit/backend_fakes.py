"""A scripted ChatBackend for driving the agent/chat/reviewer loops in tests.

Returns queued LLMResponses in order and records what it was asked, so a test
can assert the loop's behavior without any network or real model.
"""
from __future__ import annotations

from typing import Any

from poseidon.ai.backends.base import LLMResponse, ToolCall, ToolResult

_ZERO_USAGE = {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0}


def tool_use(*calls: ToolCall, model: str = "fake") -> LLMResponse:
    return LLMResponse("tool_use", list(calls), "",
                       {"role": "assistant", "content": "scripted"}, dict(_ZERO_USAGE), model)


def text_end(text: str, model: str = "fake") -> LLMResponse:
    return LLMResponse("end", [], text, {"role": "assistant", "content": text},
                       dict(_ZERO_USAGE), model)


def refusal(model: str = "fake") -> LLMResponse:
    return LLMResponse("refusal", [], "", {"role": "assistant", "content": ""},
                       dict(_ZERO_USAGE), model)


class FakeBackend:
    """A ChatBackend that replays queued responses."""

    model = "fake"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[Any], *, tools: list[dict[str, Any]],
                       system: str, force_tool: str | None = None,
                       max_tokens: int | None = None) -> LLMResponse:
        self.calls.append({"messages": list(messages), "force_tool": force_tool,
                           "tool_names": [t.get("name") for t in tools]})
        r = self._responses[self._i]
        self._i += 1
        return r

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        return [{"role": "user", "content": [{"tool_call_id": r.tool_call_id,
                 "content": r.content, "is_error": r.is_error} for r in results]}]

    async def aclose(self) -> None:
        return None
