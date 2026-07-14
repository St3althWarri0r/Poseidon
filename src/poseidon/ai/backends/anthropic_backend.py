"""Anthropic Messages backend — the platform's default brain.

Behavior-identical to the pre-seam ClaudeAgent._create_message: adaptive
thinking + effort, a cache-controlled system prompt, strict tools. Forced
tool_choice (the algorithm reviewer's one-shot) is incompatible with extended
thinking on Anthropic, so thinking/effort are omitted whenever force_tool is set.
"""
from __future__ import annotations

from typing import Any, cast

import anthropic

from ...core.config import AIConfig
from ...core.errors import AgentError
from .base import LLMResponse, StopReason, ToolCall, ToolResult


def _map_stop(stop_reason: str | None, calls: list[ToolCall]) -> StopReason:
    if stop_reason == "refusal":
        return "refusal"
    if stop_reason == "pause_turn":
        return "pause"
    if calls or stop_reason == "tool_use":
        return "tool_use"
    return "end"


class AnthropicBackend:
    def __init__(self, cfg: AIConfig, api_key: str,
                 *, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.model = cfg.model
        self._cfg = cfg
        self._client = client if client is not None else anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=3)

    async def complete(self, messages: list[Any], *, tools: list[dict[str, Any]],
                       system: str, force_tool: str | None = None,
                       max_tokens: int | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self._cfg.max_tokens,
            "system": cast("Any", [{"type": "text", "text": system,
                                    "cache_control": {"type": "ephemeral"}}]),
            "tools": cast("Any", tools),
            "messages": cast("Any", messages),
        }
        if force_tool is not None:
            kwargs["tool_choice"] = cast("Any", {"type": "tool", "name": force_tool})
        else:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self._cfg.effort}
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise AgentError(f"Anthropic authentication failed: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise AgentError(f"Anthropic rate limited after SDK retries: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AgentError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError(f"cannot reach Anthropic API: {exc}") from exc

        calls = [ToolCall(b.id, b.name, dict(b.input))
                 for b in resp.content if b.type == "tool_use"]
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        return LLMResponse(_map_stop(resp.stop_reason, calls), calls, text,
                           {"role": "assistant", "content": resp.content}, usage, resp.model)

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        return [{"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": r.tool_call_id,
            "content": r.content, "is_error": r.is_error} for r in results]}]

    async def aclose(self) -> None:
        await self._client.close()
