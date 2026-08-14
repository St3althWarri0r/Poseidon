# CLAUDE.md — `poseidon.ai`

This file provides guidance to Claude Code (claude.ai/code) when working in the AI subsystem. It supplements the repository-root `CLAUDE.md`; the invariants there (live-data-only, one order path, `Decimal` money, audit-append, secrets-in-vault) still apply.

## What this package is

The AI layer where **Claude acts as the portfolio manager**. Two entry points share the same `anthropic.AsyncAnthropic` client, tool schemas, and honesty rules:

- **`agent.py` (`ClaudeAgent.run_cycle`)** — one review cycle → a validated `Decision`.
- **`chat.py` (`ChatService.send`)** — the dashboard AI Desk conversation.

Plus `tools.py` (`ToolDispatcher`, the live-data tools Claude calls), `schemas.py` (tool JSON Schemas), `reviewer.py` (converts pasted external algorithms), and `reports.py` (renders a decision into a human report).

## The safety contract this package enforces

These are the reasons the code is shaped the way it is — preserve them:

1. **Manual tool-use loop, not the SDK tool runner.** Both `run_cycle` and `_run_tool_loop` drive the loop by hand so that (a) every tool call passes through the audited `ToolDispatcher`, and (b) the decision arrives through the strict-schema `submit_decision` tool. Do not replace this with `client.beta.messages.tool_runner(...)` — it would bypass both guarantees.
2. **Chat can never trade.** The cycle agent gets `ALL_TOOLS` (= `DATA_TOOLS` + `submit_decision`); chat gets `DATA_TOOLS` only. Nothing in the dispatcher places an order. The one mutation chat can make is `propose_algorithm`, which saves a **draft** the operator must activate. Keep `submit_decision` out of any chat-reachable tool list.
3. **A malformed decision cannot slip through.** `submit_decision` is `strict: True` + `additionalProperties: False`. `_parse_decision` then voids **all** trades if any single trade is malformed (coupled hedge/rebalance legs must not partially execute) or if trades arrive without a rationale (explainability is mandatory). Quantities are parsed as `Decimal` and rejected unless finite and positive.
4. **Provenance isolation.** The review cycle and chat each get their **own** `ToolDispatcher` instance. `dispatcher.sources_used` is snapshotted into the audited decision's `data_sources`; a concurrent chat tool call must not inject sources into that record.
5. **Prompt-injection defense in chat.** `_SESSION_CONTEXT_TAG` strips/neutralizes any `<session_context>` tag from the operator's message so a pasted message cannot forge or close the trusted platform-state block that `send()` prepends. Trust the block, never the message, for platform state.
6. **Tools never fabricate data.** `ToolDispatcher.dispatch` catches `DataError` and returns `(json, is_error=True)` carrying an explicit "do not estimate; record it in `data_gaps`" instruction — it never raises into the loop and never returns a synthesized value. Oversized results are truncated to a valid JSON envelope, never a mid-token slice of a price (a cut `412.87` → `412.8` reads as a plausible-but-wrong quote).

## Anthropic SDK conventions (current API — keep these exact)

The SDK usage here matches the current Messages API; don't "modernize" it back to deprecated shapes:

- **Thinking + effort:** `thinking={"type": "adaptive"}` with `output_config={"effort": cfg.effort}`. This is adaptive thinking (GA) — do **not** reintroduce `thinking={"type": "enabled", "budget_tokens": N}` (400s on Opus 4.6+/4.7/4.8 and Fable 5). The configured `ai.model` must therefore be a model that supports adaptive thinking and `effort` (Opus 4.6+, Sonnet 4.6, or Fable 5).
- **Prompt cache stays warm by design.** `SYSTEM_PROMPT`/`CHAT_SYSTEM_PROMPT` are frozen and marked `cache_control={"type": "ephemeral"}`; the tool list is deterministic. All per-cycle/per-turn dynamic content (timestamps, mode, watchlist, signals, `<session_context>`) lives in the **user** turn. Caching is a prefix match — never interpolate a timestamp or per-request value into the system prompt, or the cache invalidates every request.
- **Stop reasons the loop must handle:** `refusal` → raise `AgentRefusedError` (cycle) / return a canned reply (chat); never read `content` as a decision. `pause_turn` → append the assistant turn and `continue`. No `tool_use` blocks → treat as an explicit no-action cycle.
- **Strict tools:** `strict: True` sits on the tool definition alongside `name`/`description`/`input_schema` (not on `tool_choice`), and the schema needs `additionalProperties: False` + a complete `required` list. `reviewer.py` additionally forces its single tool with `tool_choice={"type": "tool", "name": "submit_algorithm_review"}`.
- **Usage metering:** accumulate `input_tokens`, `output_tokens`, `cache_read_input_tokens`, and `cache_creation_input_tokens` from `response.usage`. Usage is metered even when a cycle/turn aborts mid-loop (`last_cycle_usage()` / the `exc.usage` handoff) so the monthly budget in `app.py` is never under-counted.

## Adding a tool

1. Add the schema in `schemas.py` via `_simple_tool(...)`. Put it in `DATA_TOOLS` if both the cycle and chat should have it (read-only live data); only `submit_decision` lives in `ALL_TOOLS` beyond that. Prices and quantities are **string** in the schema to preserve `Decimal` precision.
2. Implement `async def _tool_<name>(self, ...)` on `ToolDispatcher` — dispatch resolves handlers by the `_tool_<name>` convention.
3. Record provenance for anything that touches market data: `self.sources_used.add(<result>.source)`.
4. Raise `DataError` when live data is unavailable — never return a placeholder. The dispatcher turns it into the standard "record in `data_gaps`, do not trade" tool error.

## reviewer.py

One-shot review with a **single** validation retry: the produced `poseidon_source` is checked with the workshop's own `validate_algorithm` static screen (same as on save); on failure the validator's errors are handed back once via a `tool_result`, then whatever comes back is returned with `validation_errors`. Nothing here activates an algorithm — results are drafts for the operator.
