# Design: Local-model trading brain (pluggable AI backend)

- **Date:** 2026-07-14
- **Status:** Approved (design) — pending spec review
- **Scope:** Let Poseidon's AI layer drive review cycles and operator chat with a
  local, OpenAI-compatible model (LM Studio) instead of the Anthropic API, selected
  by config, without changing any safety-critical code. Plus enable a real-time
  data feed so trades can actually clear the freshness gate.

## 1. Summary

Poseidon's portfolio-manager brain is hardwired to the Anthropic API
(`anthropic.AsyncAnthropic`). When that API key has no credit, every review cycle
fails and nothing can trade. The user wants free, autonomous paper trading powered
by the local models already running in LM Studio (Devstral-24B, Qwen3-Coder-30B, …).

This design introduces a single seam — a **`ChatBackend`** interface that performs
one LLM round-trip and returns a *normalized* result — with two implementations:
`AnthropicBackend` (today's behavior, unchanged) and `OpenAICompatibleBackend`
(LM Studio and any OpenAI-compatible endpoint). The safety-critical machinery
(audited `ToolDispatcher`, strict `submit_decision`, `_parse_decision`, provenance
isolation, chat-can't-trade) sits *above* the seam and is untouched. The backend is
chosen in config; Anthropic stays the default, so existing behavior is preserved
until the operator flips a switch.

Separately but in the same change, we enable the already-implemented
`AlpacaDataProvider` (real-time IEX feed, free, reusing the broker's `alpaca_keys`
vault credential). This fixes the structural data-freshness wall: the free
providers refresh ~once/minute and never clear the 5-second real-time gate, so
today no order can be validated regardless of which brain runs.

## 2. Motivation

Two independent gaps block paper trading today:

1. **No AI brain.** `ai.model = claude-opus-4-8`, and the Anthropic key's credit
   balance is zero → `Anthropic API error 400: credit balance too low` on every
   cycle.
2. **No real-time data.** `real_time_max_age_seconds: 5`, but finnhub/twelvedata/
   alphavantage free tiers return quotes ~30–60 s stale that never move across
   retries. `DataRouter`'s freshness policy (correctly) refuses them, so order
   validation can't get a fresh reference price.

The user has LM Studio running with capable models and has explicitly authorized
using/installing local models for trading. A local brain solves (1) for free and
unlimited; enabling Alpaca's IEX feed solves (2) with credentials already present.

**Proven feasible:** a two-round manual tool loop against
`devstral-small-2-24b-instruct-2512` via `http://localhost:1234/v1/chat/completions`
correctly emitted `get_quote(symbol="AAPL")`, then after the quote was fed back,
returned a schema-valid `submit_decision` (buy 10 AAPL @ 229.55 — the live ask,
not invented; prices as strings; thesis + confidence). That output parses cleanly
through the existing `_parse_decision`.

## 3. Goals / Non-goals

**Goals**
- A config-selected AI backend; `anthropic` (default) and `openai_compatible`.
- Zero change to the risk-gated decision path and the six `ai/` safety-contract
  properties.
- Both `run_cycle` (trading) and `ChatService.send` (operator chat) and
  `reviewer.py` route through the same backend.
- Enable real-time data via `AlpacaDataProvider` (IEX), reusing `alpaca_keys`.
- Full existing gate stays green: `ruff`, `mypy --strict`, `pytest`, `ui_verify.py`.
- No network in tests (httpx `MockTransport` + a fake backend).

**Non-goals (this change)**
- Improving *decision quality* of local models (prompt tuning, model bench-off,
  a backtest-validation loop) — explicit follow-ons.
- Streaming responses, multi-model routing, or fine-tuning.
- Any change to broker execution, the risk engine, or storage.
- Using local models for real-money trading (paper only; see Risks).

## 4. Current architecture (what exists)

- `ai/agent.py` `ClaudeAgent.run_cycle` — a **hand-driven** tool loop:
  `_create_message(messages)` → inspect `response.stop_reason` +
  `response.content` blocks (`tool_use`/`text`) → dispatch each `tool_use`
  through `ToolDispatcher` → append Anthropic-shaped `tool_result` blocks →
  capture `submit_decision.input` → `_parse_decision`.
- `ai/chat.py` `ChatService.send` — same loop shape with `DATA_TOOLS` only,
  returns text (never trades).
- `ai/reviewer.py` — one-shot, forces a single tool via
  `tool_choice={"type":"tool","name":"submit_algorithm_review"}`.
- All three share one `anthropic.AsyncAnthropic` client (`agent.client`).
- `ai/schemas.py` — `ALL_TOOLS` (= `DATA_TOOLS` + `submit_decision`), Anthropic
  tool shape: `{name, description, input_schema, strict}`.
- `core/config.py` `AIConfig` (StrictModel): `model`, `effort`, `max_tokens`,
  `api_key_credential`, `max_tool_iterations`, `review_interval_seconds`,
  price/budget fields.
- `data/providers/` — `BUILTIN_PROVIDERS` registry already includes
  `AlpacaDataProvider` (`feed` option defaults to `iex`).
- Active broker set by `poseidon.local.yaml` overlay: `alpaca`, paper, credential
  `alpaca_keys`.

## 5. Design overview

```
run_cycle / chat.send / reviewer   (UNCHANGED logic: audited dispatch,
        │                            submit_decision, _parse_decision, provenance)
        ▼
   ChatBackend.complete(messages, tools, system, force_tool?) -> LLMResponse
        │                         ▲
        ├── AnthropicBackend  ────┤   (messages.create; thinking+effort; cache;
        │                         │    strict tools; content-block parsing)
        └── OpenAICompatibleBackend   (POST /v1/chat/completions; tool_calls;
                                       no thinking/cache; strict best-effort)
```

The loop is rewritten once to speak a **normalized** vocabulary; each backend
translates that to/from its wire protocol. Nothing below the seam changes.

## 6. Detailed design

### 6.1 Normalized types — `ai/backends/base.py`

```python
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
    stop_reason: Literal["tool_use", "pause", "refusal", "end"]
    tool_calls: list[ToolCall]
    text: str
    assistant_message: Any          # backend-native turn to append to history
    usage: dict[str, int]           # input_tokens, output_tokens,
                                     # cache_read_tokens, cache_write_tokens
    model: str
```

### 6.2 `ChatBackend` protocol

```python
class ChatBackend(Protocol):
    model: str
    async def complete(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]],     # canonical (Anthropic-shaped) tool defs
        system: str,
        force_tool: str | None = None,
    ) -> LLMResponse: ...

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]: ...
    # native turn(s) to append: Anthropic → 1 user msg with tool_result blocks;
    # OpenAI → N role:"tool" messages (one per result).

    async def aclose(self) -> None: ...
```

The initial user turn (`{"role": "user", "content": <prompt string>}`) is shape-
compatible with both wire formats, so the loop still builds it directly. Only the
assistant turn and tool-result turns are backend-native, and those are produced by
the backend (`assistant_message`, `tool_result_messages`).

### 6.3 `AnthropicBackend`

Extracted verbatim from today's `_create_message` — behavior-identical:
- `messages.create(model, max_tokens, thinking={"type":"adaptive"},
  output_config={"effort": cfg.effort}, system=[cached SYSTEM_PROMPT], tools, messages)`.
- `force_tool` → `tool_choice={"type":"tool","name":force_tool}` (reviewer path).
- Map `response.content` → `tool_calls` (blocks with `type=="tool_use"` →
  `ToolCall(id, name, dict(input))`), `text` (join `type=="text"`),
  `stop_reason` (`refusal`→`refusal`, `pause_turn`→`pause`, `tool_use`→`tool_use`,
  else→`end`), `assistant_message = {"role":"assistant","content":response.content}`,
  `usage` from `response.usage`.
- `tool_result_messages(results)` → `[{"role":"user","content":[{"type":
  "tool_result","tool_use_id":r.tool_call_id,"content":r.content,
  "is_error":r.is_error} for r in results]}]`.
- Preserves the existing `anthropic.*Error` → `AgentError` mapping.

### 6.4 `OpenAICompatibleBackend`

- `httpx.AsyncClient(base_url=cfg.base_url)`; POST `/chat/completions` with
  `{model, messages: [ {"role":"system","content":system}, *messages ],
  tools: _to_openai_tools(tools), tool_choice, temperature: cfg.temperature,
  max_tokens: cfg.max_tokens}`. System is injected per call (not stored in the
  loop's history), so it can't duplicate across iterations.
- `_to_openai_tools`: `{name, description, input_schema, strict}` →
  `{"type":"function","function":{"name","description","parameters":input_schema,
  "strict":bool}}`. `strict` is best-effort (LM Studio may ignore it); correctness
  does **not** depend on it — `_parse_decision` is the backstop. If a `strict:true`
  request is rejected, retry once with `strict:false`.
- `force_tool` → `tool_choice={"type":"function","function":{"name":force_tool}}`;
  else `"auto"`.
- Parse `choices[0]`: `message.tool_calls` → `ToolCall(id, function.name,
  json.loads(function.arguments))` (a JSON-decode failure on arguments → drop that
  call and mark it so the loop can no-op safely, never fabricate); `text =
  message.content or ""`; `finish_reason` map: `tool_calls`→`tool_use`,
  `stop`→`end`, `length`→`end` (warn), `content_filter`→`refusal`; there is no
  `pause` equivalent. `assistant_message = message` (raw dict, includes
  `tool_calls`), with null `content` normalized to `""` — some OpenAI-compatible
  servers reject a re-sent assistant turn whose `content` is `null`. `usage`: `prompt_tokens`→input, `completion_tokens`→output,
  cache=0.
- `tool_result_messages(results)` → `[{"role":"tool","tool_call_id":
  r.tool_call_id,"content":r.content} for r in results]` (one per call; OpenAI
  has no per-result error flag, so `is_error` is folded into the content string,
  matching how the dispatcher already returns an explicit error envelope).
- Errors: connection/timeout/HTTP!=200 → `AgentError` (retryable set honestly),
  so the cycle fails safe exactly like an Anthropic error does today.

### 6.5 Factory — `ai/backends/__init__.py`

```python
def build_backend(cfg: AIConfig, resolve_secret) -> ChatBackend:
    if cfg.backend == "anthropic":
        return AnthropicBackend(cfg, api_key=resolve_secret(cfg.api_key_credential))
    return OpenAICompatibleBackend(cfg)   # local endpoint needs no secret
```

`app.py` builds one backend and injects it into `ClaudeAgent`, `ChatService`, and
the reviewer (replacing the shared `anthropic.AsyncAnthropic` client and the
`agent.client` property).

### 6.6 `agent.py` refactor (loop, normalized)

`ClaudeAgent.__init__(config, backend, dispatcher)`. `run_cycle` becomes:

```python
messages = [{"role": "user", "content": user_prompt}]
for iteration in range(cfg.max_tool_iterations):
    resp = await self._backend.complete(messages, tools=ALL_TOOLS, system=SYSTEM_PROMPT)
    self._record_usage(resp.usage)
    if resp.stop_reason == "refusal":
        raise AgentRefusedError(...)
    messages.append(resp.assistant_message)
    if resp.stop_reason == "pause":
        continue
    if not resp.tool_calls:
        return self._no_action_decision(...)
    results = []
    for tc in resp.tool_calls:
        if tc.name == "submit_decision":
            decision_input = tc.input
            results.append(ToolResult(tc.id, "decision recorded"))
            continue
        out, is_error = await self._dispatcher.dispatch(tc.name, tc.input)
        results.append(ToolResult(tc.id, out, is_error))
    messages.extend(self._backend.tool_result_messages(results))
    if decision_input is not None:
        return self._parse_decision(decision_input, cycle_id, resp.model)
```

`_parse_decision`, `_no_action_decision`, `_cycle_prompt`, `SYSTEM_PROMPT`, the
malformed-trade voiding and rationale-required rules — all unchanged.

### 6.7 `chat.py` refactor

Same normalization; `ChatService` takes a `backend`, passes `DATA_TOOLS`,
`CHAT_SYSTEM_PROMPT`, and its prompt-injection defenses and provenance isolation
are unchanged. Still returns text; still cannot reach `submit_decision`.

### 6.8 `reviewer.py`

Uses `backend.complete(..., force_tool="submit_algorithm_review")`, one validation
retry unchanged. Removes the direct `agent.client` dependency.

### 6.9 Config — `core/config.py` `AIConfig`

Add:
```python
backend: Literal["anthropic", "openai_compatible"] = "anthropic"
base_url: str | None = None            # required iff backend == openai_compatible
temperature: float = Field(default=0.2, ge=0.0, le=2.0)  # OpenAI path only
```
A `model_validator` enforces: `openai_compatible` ⇒ `base_url` set;
`anthropic` ⇒ `api_key_credential` non-empty. `effort`/thinking are Anthropic-only
and ignored by the OpenAI path. Defaults keep every existing config valid.

**Operator switch to local (documented, not default):**
```yaml
ai:
  backend: openai_compatible
  base_url: http://localhost:1234/v1
  model: devstral-small-2-24b-instruct-2512
  temperature: 0.2
  input_price_per_mtok: 0      # local = free
  output_price_per_mtok: 0
```

### 6.10 Real-time data — enable `AlpacaDataProvider`

Add to `data.providers` in `poseidon.yaml`, above finnhub:
```yaml
- name: alpaca
  credential: alpaca_keys       # same vault entry the broker uses
  priority: 15
  options: { feed: iex }        # free real-time IEX
```
IEX covers the mega-cap watchlist well; finnhub/twelvedata/alphavantage remain as
capability fallback (news, econ calendar, options). No code change — registry and
provider already exist.

## 7. Data flow (one autonomous cycle, local backend)

`scheduler → run_review_cycle → strategies.scan_all → agent.run_cycle`
→ `OpenAICompatibleBackend.complete` (LM Studio) → tool_calls → audited
`ToolDispatcher.dispatch` (live data via `DataRouter`, now Alpaca-IEX-first) →
tool results back → `submit_decision` → `_parse_decision` → `Decision`
→ persist + `audit.append` → `execute_decision` → **`RiskEngine` (every rule)** →
`PaperBroker`. The risk gate, audit chain, and one-order-path are identical to the
Anthropic path.

## 8. Safety preservation (the six `ai/` properties)

1. **Manual tool loop** — still hand-driven; no SDK tool-runner. ✔
2. **Chat can't trade** — chat still gets `DATA_TOOLS` only; `submit_decision`
   never in a chat tool list. ✔
3. **Malformed decision can't slip through** — `_parse_decision` unchanged; voids
   all trades on any malformed trade or missing rationale; `Decimal`-parses
   quantities. Local models are *more* likely to emit malformed output → this
   backstop matters more, and it holds. ✔
4. **Provenance isolation** — cycle and chat still get separate dispatchers;
   `sources_used` snapshot unchanged. ✔
5. **Prompt-injection defense in chat** — `_SESSION_CONTEXT_TAG` handling
   unchanged. ✔
6. **Tools never fabricate** — dispatcher unchanged; a JSON-decode failure on a
   local model's tool arguments drops the call rather than inventing values. ✔

Repo invariants: live-data-only (unchanged; Alpaca feed still flows through
`DataRouter` freshness), one order path, `Decimal` money, audit-append, secrets in
vault — all untouched.

## 9. Error handling

- Backend transport/HTTP/parse errors → `AgentError` (fail-safe: cycle logs
  `review cycle failed` and no-ops; no partial trades), identical to today's
  Anthropic error path.
- `refusal`/`content_filter` → `AgentRefusedError` (metered, recorded).
- Tool-iteration limit → existing no-action decision.
- Malformed local tool args → drop call; if it was `submit_decision`, the cycle
  reaches the iteration limit → safe no-action.

## 10. Testing strategy (no network)

- `test_backend_anthropic.py` — content-block → `LLMResponse` mapping, stop-reason
  mapping, `force_tool`, error mapping (httpx `MockTransport` under the SDK).
- `test_backend_openai.py` — request shape (system injected once, tool
  translation, `tool_choice`), `tool_calls`/`finish_reason` parsing, usage mapping,
  malformed-arguments handling, `strict:true`→`false` retry, error→`AgentError`
  (httpx `MockTransport`).
- `test_backend_parity.py` — a `FakeBackend` drives `run_cycle`/`chat.send`; assert
  identical `Decision`/text given identical normalized responses, proving the loop
  is backend-agnostic.
- Integration: one end-to-end cycle over a fake OpenAI-compatible transport +
  `FakeProvider`, asserting the decision reaches the risk engine and audit.
- Regression: the existing `ai/` suite runs unchanged against `AnthropicBackend`
  (behavior-identical extraction).
- Full gate: `ruff check src tests`, `mypy src` (strict), `pytest --cov`,
  `ui_verify.py`.

## 11. Rollout / operations

1. Land code + tests behind default `backend: anthropic` (no behavior change).
2. Add the Alpaca data provider to `poseidon.yaml`.
3. Set the `ai:` block to the local config (6.9).
4. **Operator restart required** (config + code reload; providers and the backend
   are built at startup). The assistant cannot restart the live engine (auto-mode
   classifier); a single copy-paste command is handed to the user, exactly like
   the v2.7.0 restart.
5. Verify: `/api/status` shows the engine up; `/api/quote/SPY` returns a fresh
   (<5 s) Alpaca quote; a manual `/api/cycle` in research mode produces a decision
   (no order) with `model = devstral-…`.
6. Then `autonomous` for live paper trading.

**Revert:** set `ai.backend: anthropic` (and remove the Alpaca provider line if
desired) → restart. Code is inert when unselected.

## 12. Risks & tradeoffs

- **Decision quality** is far below Opus — "reasonable," not "sharp." Acceptable
  for paper; **not** for real money. Safety is identical; only idea quality drops.
- **No adaptive thinking / prompt cache** on the local path — less deliberation;
  full system prompt re-sent each call (fine for local inference).
- **Strict structured output** isn't guaranteed by LM Studio → more frequent
  malformed decisions → more safe no-ops. Mitigated by `_parse_decision`.
- **IEX-only real-time** — thin for illiquid names; fine for the mega-cap
  watchlist. Non-quote data still uses the free providers.
- **Restart is user-performed** — unavoidable given the classifier.

## 13. Out of scope / follow-ons

- Model bench-off (Devstral vs Qwen3-Coder vs GPT-OSS) + a local-tuned prompt.
- A paper backtest/shadow-validation loop to measure decision quality before trust.
- Streaming; a settings-UI toggle for the backend; per-capability data-feed tuning.
