"""Backend seam: one LLM round-trip, normalized in and out.

The agent/chat/reviewer drive a hand-written tool loop; a ChatBackend is the
only thing that knows a wire protocol. Everything the platform's safety story
depends on (audited dispatch, strict submit_decision, _parse_decision) lives
above this seam and is backend-agnostic, so swapping Anthropic for a local
OpenAI-compatible model changes nothing about how orders are vetted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

StopReason = Literal["tool_use", "pause", "refusal", "end"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class LLMResponse:
    """Provider-agnostic result of one completion.

    ``assistant_message`` is the backend-native turn the caller appends to its
    history verbatim (Anthropic content blocks vs an OpenAI message dict); the
    caller never constructs it, so the loop stays wire-format-agnostic.
    """

    stop_reason: StopReason
    tool_calls: list[ToolCall]
    text: str
    assistant_message: Any
    usage: dict[str, int]  # input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    model: str


@runtime_checkable
class ChatBackend(Protocol):
    """One LLM round-trip. Implementations own their wire protocol."""

    model: str

    async def complete(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]],
        system: str,
        force_tool: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        """Native turn(s) carrying tool results, to append to history.

        Anthropic returns one user turn with tool_result blocks; OpenAI returns
        one ``role: "tool"`` message per result — hence a list.
        """
        ...

    async def aclose(self) -> None: ...
