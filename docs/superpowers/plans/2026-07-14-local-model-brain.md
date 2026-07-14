# Local-Model Trading Brain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Poseidon's AI layer run review cycles, operator chat, and the algorithm reviewer on a local OpenAI-compatible model (LM Studio) instead of the Anthropic API, selected by config, with the risk-gated decision path and all `ai/` safety properties untouched; and enable a real-time data feed so orders clear the freshness gate.

**Architecture:** Introduce a `ChatBackend` seam that performs one LLM round-trip and returns a normalized `LLMResponse` (text + tool-calls + stop-reason + usage). Two implementations — `AnthropicBackend` (today's behavior) and `OpenAICompatibleBackend` (LM Studio). `agent.py`/`chat.py`/`reviewer.py` call the backend and are otherwise unchanged; `app.py` builds the backend from config. Separately, enable the already-registered `AlpacaDataProvider` (real-time IEX).

**Tech Stack:** Python 3.11+ (from __future__ annotations, mypy strict), `anthropic` SDK, `httpx` (already a dep), pydantic v2, pytest-asyncio auto mode, httpx `MockTransport` for network-free tests.

## Global Constraints

- **Never break the six `ai/` safety properties** (manual tool loop; chat-can't-trade; `_parse_decision` voids malformed/rationale-less trades; provenance isolation; chat prompt-injection defense; tools never fabricate). The seam sits *above* all of them.
- **`Decimal` money end to end**; prices/quantities stay strings in tool schemas.
- **No network in tests.** httpx `MockTransport` for the OpenAI backend; an injected fake client for the Anthropic backend; `FakeProvider` for data.
- **mypy strict** must pass; `from __future__ import annotations` in every new file; ruff line length 100.
- **Default `ai.backend: anthropic`** — zero behavior change until the operator flips config.
- **Forced tool_choice is incompatible with Anthropic extended thinking** — the Anthropic backend omits `thinking`/`output_config` whenever `force_tool` is set.
- Local model default: `devstral-small-2-24b-instruct-2512`; endpoint `http://localhost:1234/v1`.

---

### Task 1: `AIConfig` backend-selection fields + validator

**Files:**
- Modify: `src/poseidon/core/config.py` (class `AIConfig`)
- Test: `tests/unit/test_config_ai_backend.py`

**Interfaces:**
- Produces: `AIConfig.backend: Literal["anthropic","openai_compatible"]`, `AIConfig.base_url: str | None`, `AIConfig.temperature: float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config_ai_backend.py
from __future__ import annotations
import pytest
from pydantic import ValidationError
from poseidon.core.config import AIConfig

def test_defaults_are_anthropic():
    c = AIConfig()
    assert c.backend == "anthropic"
    assert c.base_url is None

def test_openai_compatible_requires_base_url():
    with pytest.raises(ValidationError):
        AIConfig(backend="openai_compatible")

def test_openai_compatible_with_base_url_ok():
    c = AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1",
                 model="devstral-small-2-24b-instruct-2512")
    assert c.base_url.endswith("/v1")
    assert 0.0 <= c.temperature <= 2.0

def test_anthropic_requires_api_key_credential():
    with pytest.raises(ValidationError):
        AIConfig(backend="anthropic", api_key_credential="")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_config_ai_backend.py -q`
Expected: FAIL (`backend`/`base_url`/`temperature` are not fields yet, no validator).

- [ ] **Step 3: Add fields + validator to `AIConfig`**

In `src/poseidon/core/config.py`, add to `class AIConfig(StrictModel)` (after `api_key_credential`):

```python
    backend: Literal["anthropic", "openai_compatible"] = "anthropic"
    base_url: str | None = None            # required iff backend == openai_compatible
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)  # OpenAI path only
```

Add the validator method to the class (ensure `model_validator` is imported from pydantic):

```python
    @model_validator(mode="after")
    def _check_backend(self) -> "AIConfig":
        if self.backend == "openai_compatible" and not self.base_url:
            raise ValueError("ai.base_url is required when ai.backend is 'openai_compatible'")
        if self.backend == "anthropic" and not self.api_key_credential:
            raise ValueError("ai.api_key_credential is required when ai.backend is 'anthropic'")
        return self
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_config_ai_backend.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/core/config.py tests/unit/test_config_ai_backend.py
git commit -m "feat(config): add ai.backend selection (anthropic|openai_compatible)"
```

---

### Task 2: Normalized backend types, protocol, and `FakeBackend` test util

**Files:**
- Create: `src/poseidon/ai/backends/__init__.py` (exports; factory added in Task 5)
- Create: `src/poseidon/ai/backends/base.py`
- Create: `tests/unit/backend_fakes.py` (shared `FakeBackend` + `LLMResponse` builders)
- Test: `tests/unit/test_backend_base.py`

**Interfaces:**
- Produces: `ToolCall(id, name, input)`, `ToolResult(tool_call_id, content, is_error=False)`, `LLMResponse(stop_reason, tool_calls, text, assistant_message, usage, model)`, `ChatBackend` protocol with `complete(messages, *, tools, system, force_tool=None, max_tokens=None) -> LLMResponse`, `tool_result_messages(results) -> list`, `aclose()`.

- [ ] **Step 1: Create `base.py`**

```python
# src/poseidon/ai/backends/base.py
"""Backend seam: one LLM round-trip, normalized in and out.

The agent/chat/reviewer drive a hand-written tool loop; a ChatBackend is the
only thing that knows a wire protocol. Everything the platform's safety story
depends on (audited dispatch, strict submit_decision, _parse_decision) lives
above this seam and is backend-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    stop_reason: StopReason
    tool_calls: list[ToolCall]
    text: str
    assistant_message: Any            # backend-native turn to append to history
    usage: dict[str, int]             # input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    model: str


@runtime_checkable
class ChatBackend(Protocol):
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

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]: ...

    async def aclose(self) -> None: ...
```

- [ ] **Step 2: Create `tests/unit/backend_fakes.py`**

```python
# tests/unit/backend_fakes.py
"""A scripted ChatBackend for driving the agent/chat/reviewer loops in tests."""
from __future__ import annotations

from typing import Any

from poseidon.ai.backends.base import LLMResponse, ToolCall, ToolResult


def tool_use(*calls: ToolCall, model: str = "fake") -> LLMResponse:
    return LLMResponse("tool_use", list(calls), "", {"role": "assistant", "content": "scripted"},
                       {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                        "cache_write_tokens": 0}, model)


def text_end(text: str, model: str = "fake") -> LLMResponse:
    return LLMResponse("end", [], text, {"role": "assistant", "content": text},
                       {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                        "cache_write_tokens": 0}, model)


def refusal(model: str = "fake") -> LLMResponse:
    return LLMResponse("refusal", [], "", {"role": "assistant", "content": ""},
                       {"input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0,
                        "cache_write_tokens": 0}, model)


class FakeBackend:
    """Returns queued LLMResponses; records what it was asked."""
    model = "fake"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[Any], *, tools: list[dict[str, Any]],
                       system: str, force_tool: str | None = None,
                       max_tokens: int | None = None) -> LLMResponse:
        self.calls.append({"messages": list(messages), "force_tool": force_tool})
        r = self._responses[self._i]
        self._i += 1
        return r

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        return [{"role": "user", "content": [{"tool_call_id": r.tool_call_id,
                 "content": r.content, "is_error": r.is_error} for r in results]}]

    async def aclose(self) -> None:
        return None
```

- [ ] **Step 3: Write + run the base test**

```python
# tests/unit/test_backend_base.py
from __future__ import annotations
from poseidon.ai.backends.base import ChatBackend, LLMResponse, ToolCall, ToolResult
from tests.unit.backend_fakes import FakeBackend, tool_use

def test_toolcall_and_result_construct():
    tc = ToolCall(id="x", name="get_quote", input={"symbol": "AAPL"})
    assert tc.input["symbol"] == "AAPL"
    assert ToolResult("x", "ok").is_error is False

async def test_fakebackend_is_a_chatbackend():
    fb = FakeBackend([tool_use(ToolCall("1", "get_quote", {"symbol": "AAPL"}))])
    assert isinstance(fb, ChatBackend)
    resp = await fb.complete([], tools=[], system="s")
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].name == "get_quote"
```

Run: `.venv/bin/pytest tests/unit/test_backend_base.py -q` → Expected: PASS.
(`__init__.py` may be empty for now: `# backends package`.)

- [ ] **Step 4: Commit**

```bash
git add src/poseidon/ai/backends/ tests/unit/backend_fakes.py tests/unit/test_backend_base.py
git commit -m "feat(ai): normalized ChatBackend types + protocol + fake backend"
```

---

### Task 3: `OpenAICompatibleBackend`

**Files:**
- Create: `src/poseidon/ai/backends/openai_backend.py`
- Test: `tests/unit/test_backend_openai.py`

**Interfaces:**
- Consumes: `AIConfig`, `LLMResponse`/`ToolCall`/`ToolResult` (Task 2).
- Produces: `OpenAICompatibleBackend(cfg, *, transport=None)`.

- [ ] **Step 1: Write the failing test** (httpx MockTransport, no network)

```python
# tests/unit/test_backend_openai.py
from __future__ import annotations
import json
import httpx
import pytest
from poseidon.core.config import AIConfig
from poseidon.core.errors import AgentError
from poseidon.ai.backends.base import ToolResult
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend

def _cfg():
    return AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1",
                    model="devstral", temperature=0.2)

def _backend(handler):
    return OpenAICompatibleBackend(_cfg(), transport=httpx.MockTransport(handler))

async def test_tool_call_parsed_and_system_injected():
    captured = {}
    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"model": "devstral", "choices": [{"finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_quote", "arguments": "{\"symbol\": \"AAPL\"}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    b = _backend(handler)
    tools = [{"name": "get_quote", "description": "q", "input_schema": {"type": "object",
              "properties": {"symbol": {"type": "string"}}, "required": ["symbol"],
              "additionalProperties": False}, "strict": True}]
    resp = await b.complete([{"role": "user", "content": "hi"}], tools=tools, system="SYS")
    assert captured["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert captured["body"]["tools"][0]["type"] == "function"
    assert captured["body"]["tools"][0]["function"]["name"] == "get_quote"
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].input == {"symbol": "AAPL"}
    assert resp.usage["input_tokens"] == 10 and resp.usage["output_tokens"] == 5
    assert resp.assistant_message["content"] == ""   # null normalized
    await b.aclose()

async def test_plain_text_end():
    def handler(req):
        return httpx.Response(200, json={"model": "devstral", "choices": [{"finish_reason": "stop",
            "message": {"role": "assistant", "content": "no action"}}], "usage": {}})
    resp = await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert resp.stop_reason == "end" and resp.text == "no action"

async def test_unparseable_arguments_dropped_not_fabricated():
    def handler(req):
        return httpx.Response(200, json={"model": "d", "choices": [{"finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_quote", "arguments": "{bad"}}]}}],
            "usage": {}})
    resp = await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert resp.tool_calls == []       # dropped, never invented

async def test_http_error_becomes_agenterror():
    def handler(req):
        return httpx.Response(500, json={"error": "boom"})
    with pytest.raises(AgentError):
        await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")

def test_tool_result_messages_one_per_call():
    b = OpenAICompatibleBackend(_cfg(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    msgs = b.tool_result_messages([ToolResult("c1", "quote json"), ToolResult("c2", "err", True)])
    assert [m["role"] for m in msgs] == ["tool", "tool"]
    assert msgs[0]["tool_call_id"] == "c1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_backend_openai.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement `openai_backend.py`**

```python
# src/poseidon/ai/backends/openai_backend.py
"""OpenAI-compatible chat backend (LM Studio and equivalents).

Speaks /chat/completions with function tools. Anthropic-only features
(adaptive thinking, prompt cache) have no equivalent and are simply absent;
correctness never depends on server-side strict tool enforcement — the loop's
_parse_decision voids anything malformed.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from ..core.config import AIConfig  # noqa: TID  (adjust to `from ...core.config import AIConfig`)
from ..core.errors import AgentError
from .base import LLMResponse, StopReason, ToolCall, ToolResult

log = structlog.get_logger(__name__)


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t["input_schema"]}} for t in tools]


def _map_finish(finish_reason: str | None, calls: list[ToolCall]) -> StopReason:
    if calls or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "content_filter":
        return "refusal"
    return "end"


class OpenAICompatibleBackend:
    def __init__(self, cfg: AIConfig, *, transport: httpx.BaseTransport | None = None) -> None:
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
            payload["tool_choice"] = ({"type": "function", "function": {"name": force_tool}}
                                      if force_tool else "auto")
        try:
            r = await self._client.post("/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentError(f"local model backend error: {exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        msg = dict(choice.get("message") or {})
        if msg.get("content") is None:
            msg["content"] = ""                       # re-send compatibility
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                log.warning("dropping tool call with unparseable arguments", name=fn.get("name"))
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
```

(Note: fix the relative import to `from ...core.config import AIConfig` / `from ...core.errors import AgentError` — `backends/` is one level deeper than `ai/`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_backend_openai.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/ai/backends/openai_backend.py tests/unit/test_backend_openai.py
git commit -m "feat(ai): OpenAI-compatible chat backend for local models"
```

---

### Task 4: `AnthropicBackend` (behavior-identical extraction)

**Files:**
- Create: `src/poseidon/ai/backends/anthropic_backend.py`
- Test: `tests/unit/test_backend_anthropic.py`

**Interfaces:**
- Produces: `AnthropicBackend(cfg, api_key, *, client=None)`; injectable `client` for tests.

- [ ] **Step 1: Write the failing test** (inject a fake anthropic client — no network)

```python
# tests/unit/test_backend_anthropic.py
from __future__ import annotations
from types import SimpleNamespace
import pytest
from poseidon.core.config import AIConfig
from poseidon.ai.backends.anthropic_backend import AnthropicBackend

class _FakeMessages:
    def __init__(self, resp, sink): self._resp, self._sink = resp, sink
    async def create(self, **kwargs):
        self._sink.update(kwargs); return self._resp
class _FakeClient:
    def __init__(self, resp, sink): self.messages = _FakeMessages(resp, sink)
    async def close(self): pass

def _resp(stop, content, model="claude-x"):
    return SimpleNamespace(stop_reason=stop, content=content, model=model,
        usage=SimpleNamespace(input_tokens=7, output_tokens=3,
            cache_read_input_tokens=0, cache_creation_input_tokens=0))

def _block(**kw): return SimpleNamespace(**kw)

async def test_maps_tool_use_and_keeps_thinking_without_force():
    sink: dict = {}
    resp = _resp("tool_use", [_block(type="tool_use", id="1", name="get_quote", input={"symbol": "AAPL"})])
    b = AnthropicBackend(AIConfig(), api_key="k", client=_FakeClient(resp, sink))
    out = await b.complete([{"role": "user", "content": "hi"}], tools=[{"name": "get_quote"}], system="SYS")
    assert out.stop_reason == "tool_use"
    assert out.tool_calls[0].input == {"symbol": "AAPL"}
    assert out.usage["input_tokens"] == 7
    assert sink["thinking"] == {"type": "adaptive"}          # thinking on
    assert sink["system"][0]["text"] == "SYS"

async def test_force_tool_omits_thinking():
    sink: dict = {}
    resp = _resp("tool_use", [_block(type="tool_use", id="1", name="rev", input={})])
    b = AnthropicBackend(AIConfig(), api_key="k", client=_FakeClient(resp, sink))
    await b.complete([{"role": "user", "content": "x"}], tools=[{"name": "rev"}], system="S", force_tool="rev")
    assert "thinking" not in sink                             # forced tool ⇒ no thinking
    assert sink["tool_choice"] == {"type": "tool", "name": "rev"}

async def test_refusal_and_text_mapping():
    b = AnthropicBackend(AIConfig(), api_key="k",
                         client=_FakeClient(_resp("refusal", []), {}))
    assert (await b.complete([], tools=[], system="s")).stop_reason == "refusal"
    b2 = AnthropicBackend(AIConfig(), api_key="k",
                          client=_FakeClient(_resp("end_turn", [_block(type="text", text="hello")]), {}))
    out = await b2.complete([], tools=[], system="s")
    assert out.stop_reason == "end" and out.text == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_backend_anthropic.py -q` → Expected: FAIL (module missing).

- [ ] **Step 3: Implement `anthropic_backend.py`**

```python
# src/poseidon/ai/backends/anthropic_backend.py
"""Anthropic Messages backend — the platform's default brain.

Behavior-identical to the pre-seam ClaudeAgent._create_message: adaptive
thinking + effort, cache-controlled system prompt, strict tools. Forced
tool_choice (reviewer) is incompatible with extended thinking, so thinking is
omitted whenever force_tool is set.
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
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)

    async def complete(self, messages: list[Any], *, tools: list[dict[str, Any]],
                       system: str, force_tool: str | None = None,
                       max_tokens: int | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens or self._cfg.max_tokens,
            system=cast("Any", [{"type": "text", "text": system,
                                 "cache_control": {"type": "ephemeral"}}]),
            tools=cast("Any", tools),
            messages=cast("Any", messages),
        )
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
```

- [ ] **Step 4: Run test to verify it passes** → `.venv/bin/pytest tests/unit/test_backend_anthropic.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/ai/backends/anthropic_backend.py tests/unit/test_backend_anthropic.py
git commit -m "feat(ai): Anthropic backend (behavior-identical extraction behind the seam)"
```

---

### Task 5: `build_backend` factory

**Files:**
- Modify: `src/poseidon/ai/backends/__init__.py`
- Test: `tests/unit/test_backend_factory.py`

**Interfaces:**
- Produces: `build_backend(cfg: AIConfig, resolve_secret: Callable[[str], str]) -> ChatBackend`; re-exports `ChatBackend, LLMResponse, ToolCall, ToolResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_backend_factory.py
from __future__ import annotations
from poseidon.core.config import AIConfig
from poseidon.ai.backends import build_backend
from poseidon.ai.backends.anthropic_backend import AnthropicBackend
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend

def test_builds_anthropic_and_resolves_secret():
    seen = []
    b = build_backend(AIConfig(), lambda name: seen.append(name) or "sk-test")
    assert isinstance(b, AnthropicBackend)
    assert seen == ["anthropic_api_key"]

def test_builds_openai_without_secret_lookup():
    seen = []
    cfg = AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1", model="devstral")
    b = build_backend(cfg, lambda name: seen.append(name) or "x")
    assert isinstance(b, OpenAICompatibleBackend)
    assert seen == []                    # local backend needs no secret
```

- [ ] **Step 2: Run to verify it fails** → `.venv/bin/pytest tests/unit/test_backend_factory.py -q` → FAIL (`build_backend` missing).

- [ ] **Step 3: Implement the factory**

```python
# src/poseidon/ai/backends/__init__.py
"""Pluggable LLM backends for the AI layer."""
from __future__ import annotations

from collections.abc import Callable

from ...core.config import AIConfig
from .anthropic_backend import AnthropicBackend
from .base import ChatBackend, LLMResponse, ToolCall, ToolResult
from .openai_backend import OpenAICompatibleBackend

__all__ = ["ChatBackend", "LLMResponse", "ToolCall", "ToolResult", "build_backend"]


def build_backend(cfg: AIConfig, resolve_secret: Callable[[str], str]) -> ChatBackend:
    if cfg.backend == "anthropic":
        return AnthropicBackend(cfg, api_key=resolve_secret(cfg.api_key_credential))
    return OpenAICompatibleBackend(cfg)
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/ai/backends/__init__.py tests/unit/test_backend_factory.py
git commit -m "feat(ai): build_backend factory selects backend by config"
```

---

### Task 6: Refactor `ClaudeAgent` onto the backend

**Files:**
- Modify: `src/poseidon/ai/agent.py`
- Test: `tests/unit/test_agent_backend.py`

**Interfaces:**
- Consumes: `ChatBackend`, `ToolResult`, `FakeBackend`.
- Produces: `ClaudeAgent(config, backend, dispatcher)` (was `(config, api_key, dispatcher)`); drops `.client` property and `_create_message`.

- [ ] **Step 1: Write the failing test** (drive run_cycle over FakeBackend)

```python
# tests/unit/test_agent_backend.py
from __future__ import annotations
from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.backends.base import ToolCall
from poseidon.core.config import AIConfig
from poseidon.core.enums import DecisionAction, TradingMode
from tests.unit.backend_fakes import FakeBackend, tool_use, text_end
from tests.conftest import make_dispatcher  # assumes an existing helper; else build ToolDispatcher with FakeProvider

def _agent(responses):
    return ClaudeAgent(AIConfig(), FakeBackend(responses), make_dispatcher())

async def test_submit_decision_flows_to_parse():
    responses = [tool_use(ToolCall("d1", "submit_decision",
                 {"action": "no_action", "summary": "flat", "trades": []}))]
    d = await _agent(responses).run_cycle(mode=TradingMode.RESEARCH, watchlist=["AAPL"],
             enabled_strategies=[], strategy_signals=[], market_session="regular")
    assert d.action == DecisionAction.NO_ACTION

async def test_no_tool_calls_is_no_action():
    d = await _agent([text_end("nothing to do")]).run_cycle(mode=TradingMode.RESEARCH,
             watchlist=[], enabled_strategies=[], strategy_signals=[], market_session="regular")
    assert d.action == DecisionAction.NO_ACTION
```

(If `make_dispatcher` doesn't exist in conftest, construct a `ToolDispatcher` with the repo's `FakeProvider`-backed `DataRouter` the same way existing agent tests do; check `tests/unit/test_agent*.py` for the established pattern and reuse it.)

- [ ] **Step 2: Run to verify it fails** → FAIL (`ClaudeAgent.__init__` still takes `api_key`).

- [ ] **Step 3: Refactor `agent.py`**

Change the imports: remove `import anthropic`; add `from .backends.base import ChatBackend, ToolResult`.

Replace `__init__`, `run_cycle`'s loop, `_record_usage`, and delete `_create_message` + the `client` property:

```python
    def __init__(self, config: AIConfig, backend: ChatBackend, dispatcher: ToolDispatcher) -> None:
        self._config = config
        self._backend = backend
        self._dispatcher = dispatcher
        self._cycle_usage: dict[str, int] = {}

    # (delete the `client` property)

    def _record_usage(self, usage: dict[str, int]) -> None:
        u = self._cycle_usage
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            u[k] += usage.get(k, 0)
        u["api_calls"] += 1
```

Inside `run_cycle`, replace the loop body (was lines ~111-153) with:

```python
        for iteration in range(self._config.max_tool_iterations):
            resp = await self._backend.complete(messages, tools=ALL_TOOLS, system=SYSTEM_PROMPT)
            self._record_usage(resp.usage)

            if resp.stop_reason == "refusal":
                raise AgentRefusedError("model declined the review request; cycle skipped")

            messages.append(resp.assistant_message)

            if resp.stop_reason == "pause":
                continue

            if not resp.tool_calls:
                log.warning("cycle ended without submit_decision", cycle=cycle_id)
                return self._no_action_decision(cycle_id,
                    f"cycle ended without a decision: {resp.text[:500]}")

            results: list[ToolResult] = []
            for tc in resp.tool_calls:
                if tc.name == "submit_decision":
                    decision_input = tc.input
                    results.append(ToolResult(tc.id, "decision recorded"))
                    continue
                out, is_error = await self._dispatcher.dispatch(tc.name, tc.input)
                log.info("tool call", cycle=cycle_id, iteration=iteration, tool=tc.name, error=is_error)
                results.append(ToolResult(tc.id, out, is_error))
            messages.extend(self._backend.tool_result_messages(results))

            if decision_input is not None:
                return self._parse_decision(decision_input, cycle_id, resp.model)
```

Delete `_create_message` entirely.

- [ ] **Step 4: Run to verify it passes** → `.venv/bin/pytest tests/unit/test_agent_backend.py -q` → PASS. Also run the existing agent suite: `.venv/bin/pytest tests/unit/test_agent*.py -q` (some will fail on the constructor signature — Task 6 Step 4b).

- [ ] **Step 4b: Update existing agent tests** that construct `ClaudeAgent(cfg, api_key, dispatcher)` → wrap the key in a backend: `ClaudeAgent(cfg, AnthropicBackend(cfg, "k", client=fake), dispatcher)` or a `FakeBackend`. Prefer converting them to `FakeBackend` where they only need scripted turns. Run `.venv/bin/pytest tests/unit/test_agent*.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/ai/agent.py tests/unit/test_agent_backend.py tests/unit/test_agent*.py
git commit -m "refactor(ai): drive ClaudeAgent through the ChatBackend seam"
```

---

### Task 7: Refactor `ChatService` onto the backend

**Files:**
- Modify: `src/poseidon/ai/chat.py`
- Test: `tests/unit/test_chat_backend.py` (+ update existing chat tests)

**Interfaces:**
- Produces: `ChatService(config, backend, dispatcher, db)` (was `(config, client, dispatcher, db)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_chat_backend.py
from __future__ import annotations
from poseidon.ai.chat import ChatService
from poseidon.ai.backends.base import ToolCall
from poseidon.core.config import AIConfig
from tests.unit.backend_fakes import FakeBackend, text_end
# reuse the repo's in-memory Database + dispatcher fixtures used by existing chat tests

async def test_chat_returns_text(chat_db, chat_dispatcher):   # fixtures per existing test_chat.py
    svc = ChatService(AIConfig(), FakeBackend([text_end("hello operator")]), chat_dispatcher, chat_db)
    out = await svc.send("hi", context="mode=research")
    assert out["reply"] == "hello operator"
```

- [ ] **Step 2: Run to verify it fails** → FAIL (constructor takes `client`).

- [ ] **Step 3: Refactor `chat.py`**

Remove `import anthropic`; add `from .backends.base import ChatBackend, ToolResult`. Change `__init__` to take `backend: ChatBackend` (store `self._backend`). Replace `_run_tool_loop` internals and delete `_create_message`; make `_record_usage` take the dict:

```python
    async def _run_tool_loop(self, messages: list[dict[str, Any]],
                             usage: dict[str, int], tool_calls: list[str]) -> str:
        for _ in range(self._config.max_tool_iterations):
            resp = await self._backend.complete(messages, tools=DATA_TOOLS, system=CHAT_SYSTEM_PROMPT)
            self._record_usage(resp.usage, usage)
            if resp.stop_reason == "refusal":
                return "I can't help with that request."
            messages.append(resp.assistant_message)
            if resp.stop_reason == "pause":
                continue
            if not resp.tool_calls:
                return resp.text.strip()
            results: list[ToolResult] = []
            for tc in resp.tool_calls:
                out, is_error = await self._dispatcher.dispatch(tc.name, tc.input)
                log.info("chat tool call", tool=tc.name, error=is_error)
                tool_calls.append(tc.name)
                results.append(ToolResult(tc.id, out, is_error))
            messages.extend(self._backend.tool_result_messages(results))
        return "I hit the tool-call limit before finishing — ask me to continue."

    @staticmethod
    def _record_usage(usage_in: dict[str, int], usage: dict[str, int]) -> None:
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            usage[k] += usage_in.get(k, 0)
        usage["api_calls"] += 1
```

- [ ] **Step 4: Run to verify it passes**, update existing `test_chat*.py` constructors to pass a `FakeBackend`/`AnthropicBackend`. Run `.venv/bin/pytest tests/unit/test_chat*.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/ai/chat.py tests/unit/test_chat_backend.py tests/unit/test_chat*.py
git commit -m "refactor(ai): drive ChatService through the ChatBackend seam"
```

---

### Task 8: Refactor `review_algorithm` onto the backend

**Files:**
- Modify: `src/poseidon/ai/reviewer.py`
- Test: `tests/unit/test_reviewer_backend.py` (+ update existing reviewer tests)

**Interfaces:**
- Produces: `review_algorithm(backend, *, source, instructions="", max_tokens=8000)` (was `(client, model, *, source, ...)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reviewer_backend.py
from __future__ import annotations
from poseidon.ai.reviewer import review_algorithm
from poseidon.ai.backends.base import LLMResponse, ToolCall
from tests.unit.backend_fakes import FakeBackend

def _review_call(convertible=False):
    payload = {"analysis": "x", "risks": [], "recommendations": [], "convertible": convertible,
               "poseidon_source": None, "suggested_name": "n", "suggested_description": "d",
               "conversion_notes": "none"}
    return LLMResponse("tool_use", [ToolCall("r1", "submit_algorithm_review", payload)], "",
                       {"role": "assistant", "content": "x"},
                       {"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 0,
                        "cache_write_tokens": 0}, "fake")

async def test_reviewer_returns_review_and_meters_usage():
    out = await review_algorithm(FakeBackend([_review_call()]), source="def x(): pass")
    assert out["convertible"] is False
    assert out["validation_errors"] == []
    assert out["usage"]["input_tokens"] == 5
```

- [ ] **Step 2: Run to verify it fails** → FAIL (signature).

- [ ] **Step 3: Refactor `reviewer.py`**

Remove `import anthropic`; add `from .backends.base import ChatBackend, ToolResult`. Replace the function:

```python
async def review_algorithm(backend: ChatBackend, *, source: str, instructions: str = "",
                           max_tokens: int = 8000) -> dict[str, Any]:
    prompt = (
        "Review this algorithm and convert it to the Poseidon contract if possible.\n"
        + (f"Operator instructions: {instructions}\n" if instructions.strip() else "")
        + f"\n--- pasted algorithm ---\n{source[:40_000]}\n--- end ---"
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    usage = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}

    for attempt in (1, 2):
        resp = await backend.complete(messages, tools=[_REVIEW_TOOL], system=_SYSTEM,
                                      force_tool="submit_algorithm_review", max_tokens=max_tokens)
        usage["api_calls"] += 1
        usage["input_tokens"] += resp.usage.get("input_tokens", 0)
        usage["output_tokens"] += resp.usage.get("output_tokens", 0)
        call = next((c for c in resp.tool_calls if c.name == "submit_algorithm_review"), None)
        if call is None:
            raise AgentError("algorithm review returned no result")
        review = dict(call.input)
        produced = review.get("poseidon_source")
        problems = validate_algorithm(str(produced)) if produced else []
        if not problems or attempt == 2:
            review["validation_errors"] = problems
            review["usage"] = usage
            if problems:
                log.warning("review source failed validation after retry", problems=problems)
            return review
        messages.append(resp.assistant_message)
        messages.extend(backend.tool_result_messages([ToolResult(
            call.id,
            "The produced poseidon_source failed static validation: " + "; ".join(problems)
            + ". Call submit_algorithm_review again with corrected source.", is_error=True)]))
    raise AgentError("unreachable")  # pragma: no cover
```

- [ ] **Step 4: Run to verify it passes**; update existing reviewer tests to pass a backend. `.venv/bin/pytest tests/unit/test_reviewer*.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/ai/reviewer.py tests/unit/test_reviewer_backend.py tests/unit/test_reviewer*.py
git commit -m "refactor(ai): drive algorithm reviewer through the ChatBackend seam"
```

---

### Task 9: Wire `app.py` to build + inject + close the backend

**Files:**
- Modify: `src/poseidon/app.py` (lines ~171-172, ~184, ~779-780, shutdown path)
- Test: `tests/integration/test_app_backend_wiring.py`

**Interfaces:**
- Consumes: `build_backend` (Task 5).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_app_backend_wiring.py
from __future__ import annotations
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.ai.backends import build_backend
from poseidon.core.config import AIConfig

def test_openai_config_builds_local_backend_without_vault():
    cfg = AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1", model="devstral")
    def boom(_name): raise AssertionError("must not read the vault for a local backend")
    assert isinstance(build_backend(cfg, boom), OpenAICompatibleBackend)
```

- [ ] **Step 2: Run to verify it fails/passes** — this asserts factory behavior (passes after Task 5). It guards the wiring intent. Run: `.venv/bin/pytest tests/integration/test_app_backend_wiring.py -q`.

- [ ] **Step 3: Edit `app.py`**

Add import near the other `.ai` imports: `from .ai.backends import build_backend`.

Replace lines 171-172:
```python
        backend = build_backend(cfg.ai, self.vault.get)
        self._backend = backend
        self.agent = ClaudeAgent(cfg.ai, backend, dispatcher)
```
Replace line 184 (`self.agent.client` → `backend`):
```python
        self.chat = ChatService(cfg.ai, backend, chat_dispatcher, self.db)
```
Replace the reviewer call (lines ~779-780) — the wrapper method `review_algorithm`:
```python
        review = await review_algorithm(self._backend, source=source, instructions=instructions)
```
In the kernel shutdown/stop path (find where other async resources are closed), add:
```python
        await self._backend.aclose()
```

- [ ] **Step 4: Run to verify it passes** → PASS. Then `.venv/bin/pytest tests/ -q -k "agent or chat or reviewer or app"`.

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/app.py tests/integration/test_app_backend_wiring.py
git commit -m "feat(app): build and inject the configured ChatBackend"
```

---

### Task 10: Integration — end-to-end local-backend review cycle

**Files:**
- Create: `tests/integration/test_local_backend_cycle.py`

**Interfaces:**
- Consumes: `OpenAICompatibleBackend`, the repo integration `stack`/`FakeProvider` fixtures.

- [ ] **Step 1: Write the test** (scripted LM Studio over MockTransport; real ToolDispatcher/DataRouter with FakeProvider)

```python
# tests/integration/test_local_backend_cycle.py
from __future__ import annotations
import json
import httpx
from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.core.config import AIConfig
from poseidon.core.enums import DecisionAction, TradingMode
# reuse the integration dispatcher/provider fixtures the existing agent-cycle tests use

def _script():
    # round 1: ask get_quote; round 2: submit_decision no_action
    seq = iter([
        {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "get_portfolio", "arguments": "{}"}}]}},
        {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function", "function": {
            "name": "submit_decision",
            "arguments": json.dumps({"action": "no_action", "summary": "flat", "trades": []})}}]}},
    ])
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "devstral", "choices": [next(seq)], "usage": {}})
    return handler

async def test_local_cycle_produces_decision(cycle_dispatcher):   # fixture: ToolDispatcher over FakeProvider
    cfg = AIConfig(backend="openai_compatible", base_url="http://x/v1", model="devstral")
    backend = OpenAICompatibleBackend(cfg, transport=httpx.MockTransport(_script()))
    agent = ClaudeAgent(cfg, backend, cycle_dispatcher)
    d = await agent.run_cycle(mode=TradingMode.AUTONOMOUS, watchlist=["AAPL"],
            enabled_strategies=[], strategy_signals=[], market_session="regular")
    assert d.action == DecisionAction.NO_ACTION
    assert d.model == "devstral"
    await backend.aclose()
```

- [ ] **Step 2: Run** → `.venv/bin/pytest tests/integration/test_local_backend_cycle.py -q` → PASS. (If a get_portfolio tool needs live data, the FakeProvider fixture supplies it; match the fixture the existing cycle tests use.)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_local_backend_cycle.py
git commit -m "test(ai): end-to-end review cycle over a local OpenAI-compatible backend"
```

---

### Task 11: Enable real-time Alpaca data + document the local switch

**Files:**
- Modify: `config/poseidon.example.yaml` (uncomment Alpaca data provider; document `ai.backend`)
- Modify: `docs/api-configuration.md` (a "Local model brain" + "Real-time data" note)
- Test: `tests/unit/test_config_alpaca_provider.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config_alpaca_provider.py
from __future__ import annotations
from poseidon.core.config import AppConfig   # or the loader used elsewhere

def test_alpaca_data_provider_and_local_ai_parse():
    raw = {
        "ai": {"backend": "openai_compatible", "base_url": "http://localhost:1234/v1",
               "model": "devstral-small-2-24b-instruct-2512"},
        "data": {"providers": [{"name": "alpaca", "credential": "alpaca_keys", "priority": 15,
                                "options": {"feed": "iex"}},
                               {"name": "finnhub", "priority": 20}]},
    }
    cfg = AppConfig.model_validate(raw)      # match the actual loader signature
    names = [p.name for p in cfg.data.providers]
    assert "alpaca" in names and cfg.ai.backend == "openai_compatible"
```

(Adjust `AppConfig.model_validate` to the repo's real config-load entry point — check `core/config.py` for how providers are parsed.)

- [ ] **Step 2: Run** → PASS if the schema already supports these fields (it does — `alpaca` is in `BUILTIN_PROVIDERS`, `options` is an existing provider field). If the loader needs a tweak, make the minimal one.

- [ ] **Step 3: Update `config/poseidon.example.yaml`** — uncomment the Alpaca data provider block and add the `ai.backend` documentation:

```yaml
# data.providers: enable Alpaca's free real-time IEX feed (reuses the broker credential):
  - name: alpaca
    credential: alpaca_keys
    priority: 15
    options: { feed: iex }
# ai: to run a local model instead of the Anthropic API:
#   backend: openai_compatible
#   base_url: http://localhost:1234/v1
#   model: devstral-small-2-24b-instruct-2512
#   temperature: 0.2
#   input_price_per_mtok: 0
#   output_price_per_mtok: 0
```

- [ ] **Step 4: Add a docs note** in `docs/api-configuration.md` (a short "Local model brain (LM Studio)" section: how to switch backend, the IEX-data requirement for real-time trading, and the quality caveat).

- [ ] **Step 5: Commit**

```bash
git add config/poseidon.example.yaml docs/api-configuration.md tests/unit/test_config_alpaca_provider.py
git commit -m "docs(config): enable Alpaca IEX real-time data + document local-model backend"
```

---

### Task 12: Full gate + deploy handoff

- [ ] **Step 1: Run the full gate**

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pytest --cov -q
.venv/bin/python scripts/ui_verify.py     # if present in the repo
```
Expected: ruff clean, mypy strict clean, all tests pass. Fix anything red before proceeding.

- [ ] **Step 2: Commit any gate fixes**

```bash
git add -A && git commit -m "chore(ai): satisfy ruff/mypy for the backend seam"
```

- [ ] **Step 3: Prepare the operator's deploy steps** (NOT committed; hand to the user):

1. Edit `~/.config/poseidon/poseidon.yaml`:
   - Under `data.providers`, add (above finnhub):
     ```yaml
     - name: alpaca
       credential: alpaca_keys
       priority: 15
       options: { feed: iex }
     ```
   - Set the `ai:` block:
     ```yaml
     ai:
       backend: openai_compatible
       base_url: http://localhost:1234/v1
       model: devstral-small-2-24b-instruct-2512
       temperature: 0.2
       input_price_per_mtok: 0
       output_price_per_mtok: 0
     ```
2. Ensure LM Studio is running with `devstral-small-2-24b-instruct-2512` loaded and the server on `:1234`.
3. Restart the engine (operator-run — the assistant cannot): stop the running `poseidon run`, then relaunch it the same way as the v2.7.0 restart.
4. Verify: `curl -s 127.0.0.1:8321/api/status` shows the engine up; `curl -s 127.0.0.1:8321/api/quote/SPY` returns a **fresh** Alpaca quote (<5 s); `curl -sX POST 127.0.0.1:8321/api/cycle` in research mode produces a decision with `model=devstral-…` and no order.
5. Flip to autonomous for live paper trading.

**Revert:** set `ai.backend: anthropic` and remove the alpaca data line → restart.

## Self-Review

- **Spec coverage:** backend abstraction (T2), OpenAI backend (T3), Anthropic backend (T4), factory (T5), agent/chat/reviewer refactors (T6-8), app wiring (T9), config (T1), real-time data (T11), tests (T2-10), gate + rollout (T12). All spec sections mapped.
- **Placeholders:** none — every step shows the code or the exact command. The two "match the existing fixture" notes (T6/T7/T10) point to concrete existing patterns rather than inventing new fixtures.
- **Type consistency:** `ChatBackend.complete(messages, *, tools, system, force_tool=None, max_tokens=None)` and `tool_result_messages(list[ToolResult])` are used identically in T3, T4, T6, T7, T8; `LLMResponse` fields (`stop_reason, tool_calls, text, assistant_message, usage, model`) are consumed consistently; `ToolResult(tool_call_id, content, is_error=False)` and `ToolCall(id, name, input)` match across producers and consumers.

**Known refinement vs spec:** the OpenAI backend omits the `strict` tool flag (and its retry) for the MVP — LM Studio produced valid tool output without it in feasibility testing, and `_parse_decision` is the real backstop. Re-add `strict` as a tuning follow-on if malformed rates warrant it.
