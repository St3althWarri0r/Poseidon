"""OpenAI-compatible chat backend (LM Studio and equivalents).

Speaks ``/chat/completions`` with function tools against a local or self-hosted
endpoint. Anthropic-only features (adaptive thinking, prompt cache) have no
equivalent and are simply absent. Correctness never depends on server-side
strict tool enforcement: the agent loop's ``_parse_decision`` voids anything
malformed, and a tool call with unparseable arguments is dropped rather than
guessed — never a fabricated value.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from ...core.config import AIConfig
from ...core.errors import AgentError, BackendUnreachableError
from .base import LLMResponse, StopReason, ToolCall, ToolResult

log = structlog.get_logger(__name__)


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Poseidon's Anthropic-shaped tool defs to OpenAI function tools."""
    return [{"type": "function", "function": {
        "name": t["name"],
        "description": t.get("description", ""),
        "parameters": t["input_schema"],
    }} for t in tools]


def _map_finish(finish_reason: str | None, calls: list[ToolCall]) -> StopReason:
    if calls or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "content_filter":
        return "refusal"
    return "end"


class OpenAICompatibleBackend:
    def __init__(self, cfg: AIConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.model = cfg.model
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=(cfg.base_url or "").rstrip("/"),
            timeout=httpx.Timeout(120.0, connect=10.0),
            transport=transport,
        )

    async def complete(self, messages: list[Any], *, tools: list[dict[str, Any]],
                       system: str, force_tool: str | None = None,
                       max_tokens: int | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": self._cfg.temperature,
            "max_tokens": max_tokens or self._cfg.max_tokens,
        }
        if tools:
            payload["tools"] = _to_openai_tools(tools)
            # LM Studio (and many OpenAI-compatible servers) accept tool_choice
            # only as a string ("auto"/"required"/"none"), NOT a specific-function
            # object. force_tool is only used where exactly one tool is offered
            # (the algorithm reviewer), so "required" forces that single tool.
            payload["tool_choice"] = "required" if force_tool else "auto"
        try:
            r = await self._client.post("/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Connect-phase failure: the backend could not be reached at all
            # (e.g. LM Studio not running). ConnectError/ConnectTimeout subclass
            # httpx.HTTPError, so this branch MUST come first. A ReadTimeout
            # mid-generation or an HTTP 4xx/5xx means the server is up but
            # erroring — that stays a plain AgentError below.
            raise BackendUnreachableError(
                f"model backend unreachable at {self._client.base_url}: {exc}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentError(f"local model backend error: {exc}") from exc
        if not isinstance(data, dict):
            # A 2xx body that is valid JSON but not an object (null, a list, a
            # bare scalar) would raise AttributeError on the structural access
            # below and escape the AgentError channel the callers handle.
            raise AgentError(f"local model backend returned non-object JSON body: {type(data).__name__}")

        choice = (data.get("choices") or [{}])[0]
        msg = dict(choice.get("message") or {})
        if msg.get("content") is None:
            msg["content"] = ""  # some servers reject a re-sent assistant turn with null content
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                log.warning("dropping tool call with unparseable arguments", name=fn.get("name"))
                continue
            if not isinstance(args, dict):
                # Valid JSON that is not an object ('[]', '5', '"x"') is not a
                # usable argument mapping. submit_decision bypasses the dispatcher,
                # so a non-dict here would reach _parse_decision directly; drop it
                # rather than forward a non-mapping.
                log.warning("dropping tool call with non-object arguments", name=fn.get("name"))
                continue
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), input=args))
        usage = data.get("usage") or {}
        return LLMResponse(
            stop_reason=_map_finish(choice.get("finish_reason"), calls),
            tool_calls=calls,
            text=msg.get("content") or "",
            assistant_message=msg,
            usage={"input_tokens": usage.get("prompt_tokens", 0) or 0,
                   "output_tokens": usage.get("completion_tokens", 0) or 0,
                   "cache_read_tokens": 0, "cache_write_tokens": 0},
            model=data.get("model", self.model),
        )

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        return [{"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content}
                for r in results]

    async def aclose(self) -> None:
        await self._client.aclose()
