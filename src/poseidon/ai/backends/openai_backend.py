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


def _to_openai_tools(tools: list[dict[str, Any]], *,
                     strict: bool = False) -> list[dict[str, Any]]:
    """Translate Poseidon's Anthropic-shaped tool defs to OpenAI function tools.

    ``strict`` is carried through. ``submit_decision`` declares it alongside
    ``additionalProperties: false`` and a complete ``required`` list, and that
    is what makes a malformed decision structurally impossible on the Anthropic
    path. Dropping it here meant the local model — the backend the live config
    actually uses — received no grammar-constrained decoding, leaving
    ``required``/``enum``/``additionalProperties`` decorative. Only propagate it
    where the source tool asks for it: forcing ``strict`` onto a loose read-only
    tool schema would start rejecting valid calls.
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        fn: dict[str, Any] = {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        }
        if strict and t.get("strict"):
            fn["strict"] = True
        out.append({"type": "function", "function": fn})
    return out


# A server-side failure to parse the MODEL's own output — not a bad request.
# LM Studio + gpt-oss-20b hits this intermittently ("The model produced output
# that does not match the expected peg-native format"): measured at ~6% on a
# live deployment, 70 failures against 1074 completed cycles. The identical
# request succeeds on retry, and no property of the request reproduces it
# (prompt size, tool count, strict, max_tokens and multi-turn depth were each
# bisected against the live endpoint). Losing a whole review cycle to one bad
# generation is pure waste, so it is retried ONCE — and only this class:
# context overflow and auth are deterministic and must surface immediately.
#
# Safe to retry because a completion places no order: the order path is
# downstream of submit_decision, so a repeated request cannot double-execute.
_TRANSIENT_GENERATION_MARKERS = (
    "predict stream returned an error",
    "does not match the expected",
    "peg-native",
)

_CONTEXT_OVERFLOW_MARKERS = (
    "exceed_context_size", "context_length_exceeded",
    "exceeds the available context size", "maximum context length",
    "context window", "prompt is too long",
)


def _is_transient_generation_error(body: str) -> bool:
    lower = body.lower()
    if any(m in lower for m in _CONTEXT_OVERFLOW_MARKERS):
        return False  # deterministic — a retry burns the cycle and hides the remedy
    return any(m in lower for m in _TRANSIENT_GENERATION_MARKERS)


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

    async def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        """POST once, retrying a single time if the server failed to parse its
        own model's output. Any other status is re-raised for the caller's
        classifier."""
        for attempt in (0, 1):
            if attempt:
                # Resample rather than replay. Measured in production: the retry
                # fired and the SECOND attempt failed too, because at
                # temperature 0.2 an identical prompt reproduces a near-identical
                # generation — including the one the runtime cannot parse. The
                # retry only buys anything if it explores a different path, so
                # nudge temperature for the retry alone. The request is
                # otherwise byte-identical, and the first attempt keeps the
                # operator's configured temperature.
                payload = {**payload, "temperature": max(self._cfg.temperature, 0.7)}
            r = await self._client.post("/chat/completions", json=payload)
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError:
                body = " ".join((r.text or "").split())
                if attempt == 0 and _is_transient_generation_error(body):
                    log.warning("model produced unparseable output; retrying once",
                                status=r.status_code, body=body[:200])
                    continue
                raise
            return r
        raise AssertionError("unreachable")  # pragma: no cover

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
            payload["tools"] = _to_openai_tools(
                tools, strict=self._cfg.strict_tools)
            # LM Studio (and many OpenAI-compatible servers) accept tool_choice
            # only as a string ("auto"/"required"/"none"), NOT a specific-function
            # object. force_tool is only used where exactly one tool is offered
            # (the algorithm reviewer), so "required" forces that single tool.
            payload["tool_choice"] = "required" if force_tool else "auto"
        try:
            r = await self._post_with_retry(payload)
            data = r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Connect-phase failure: the backend could not be reached at all
            # (e.g. LM Studio not running). ConnectError/ConnectTimeout subclass
            # httpx.HTTPError, so this branch MUST come first. A ReadTimeout
            # mid-generation or an HTTP 4xx/5xx means the server is up but
            # erroring — that stays a plain AgentError below.
            raise BackendUnreachableError(
                f"model backend unreachable at {self._client.base_url}: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            # On 4xx/5xx the response body carries the server's own diagnosis
            # (e.g. LM Studio's exceed_context_size_error with token counts);
            # this message is all the operator sees in the component-error
            # notification, so surface the body — bounded and single-line — and
            # name the remedy for a too-small context window, which is a
            # host-side condition only the operator can fix. No angle brackets
            # in the added text: the desktop channel feeds notify-send, and
            # body-markup daemons (KDE) strip unknown <tags> silently.
            full = " ".join((exc.response.text or "").split())
            body = full[:400]
            detail = (f"local model backend error: HTTP {exc.response.status_code} "
                      f"from {exc.request.url.path}")
            if body:
                detail += f" — server said: {body}"
            # Vendor context-overflow signatures (LM Studio/llama.cpp type and
            # message, OpenAI code, vLLM/Anthropic-style messages), matched as
            # phrases on the UNtruncated body so incidental words in an
            # unrelated error ("contextlib", "exceeded") cannot trigger the
            # remedy and a long preamble cannot hide it.
            lower = full.lower()
            if any(marker in lower for marker in _CONTEXT_OVERFLOW_MARKERS):
                detail += (f"; fix: reload the model with a larger context length (>=32768) — "
                           f"LM Studio: App Settings > Default Context Length, or "
                           f"`lms load {self.model} --context-length 32768`")
            log.error("model backend rejected request",
                      status=exc.response.status_code, body=body)
            raise AgentError(detail) from exc
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
