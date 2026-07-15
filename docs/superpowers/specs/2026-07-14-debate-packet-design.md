# Advisory Analyst Firm → Debate Packet — Design Spec

**Date:** 2026-07-14
**Status:** Approved (design), pending implementation plan
**Target:** Poseidon v2.10.0 candidate (batched with model tiering)
**Origin:** Sub-project #3 of the cross-pollination program ([[poseidon-crosspollination-program]]),
from TradingAgents' multi-analyst + bull/bear debate structure (paper §3–4, analysis
`~/Desktop/tradingagents-poseidon-analysis.md` §3.4, with the guardrails from §4.1/§4.4/§4.5).
Depends on sub-project #2 (model tiering) — the firm runs on the utility backend.

## 1. Goal

Give the Claude portfolio manager an **explainable, multi-perspective analysis
packet** to reason over, produced by a simulated "research firm": four analysts
each write a structured report, a bull and a bear debate it, a facilitator records
a verdict, and three advisory risk voices add a risk perspective. The assembled
**`AnalysisPacket`** becomes **one additional input** to the PM's review cycle —
never a decision, never a gate.

This is the "full firm" scope: 4 analysts + multi-round bull/bear debate + a
3-persona advisory risk lens. It is opt-in infrastructure that is expensive per
run, so it is **precomputed asynchronously and cached**, and it rides the cheap
utility model from #2 (on the user's local-Devstral setup it is $0).

## 2. Invariants — the safety properties (this is the whole point)

The firm is **advisory only** and sits strictly **upstream** of the PM. It mirrors
the reflection loop's isolation exactly:

1. **The packet influences only the PM's reasoning — it never reaches the gate or
   the order path.** It is injected as context into the review-cycle *user turn*
   (like trade lessons) and is passed to nothing else: never to `RiskEngine`,
   `OrderManager._process_order`, the `submit_decision` tool schema, or the chat
   dispatcher. So it can shift *what the PM proposes* — that is its whole job — but
   it cannot widen a risk limit, approve an order, or touch execution. (The safety
   claim is this **data-flow** isolation, NOT "behavior is identical with/without a
   packet" — the packet is meant to change the proposal; see §5.)
2. **The "risk lens" is NOT the risk engine.** The three risk voices
   (aggressive / neutral / conservative) produce *advisory commentary* only. The
   code names them `RiskLens` / `risk_lens` — never `RiskEngine`/`RiskManager` —
   and every docstring states they cannot approve, size, or block a trade. The
   real pre-trade engine (caps, VaR, drawdown halt, reduce-only, circuit breaker)
   is untouched and remains the sole gate. This directly enforces analysis §4.1.
3. **Off the execution hot path.** The firm is ~12–25 model calls per symbol
   (4 analysts + a multi-round debate + the risk lens). It
   runs on a **scheduled background sweep**, stores packets, and the cycle reads
   the freshest cached packet. It never blocks a fill, an exit, or a review cycle
   (analysis §4.5). If no fresh packet exists, the PM proceeds without one.
4. **Advisory prose stays out of the tamper-evident audit chain.** Packets live
   in their own `analysis_packets` table (like `trade_lessons`). A one-line
   `audit.append("ai", "analysis_packet_written", {...})` marker is fine (a fact:
   "a packet was produced"), but the packet prose itself is not an auditable fact.
5. **Untrusted input is quarantined.** The market-sentiment analyst reads only
   trusted, already-ingested data in v1 — news tone plus price/volume momentum
   from the pinned snapshot — with **no external social feed**. The external
   *text* it consumes (news) passes the existing prompt-injection scanner and is
   labeled weak evidence, never a trigger; a hardened social connector can later
   reuse that same quarantined path (analysis §4.4).
6. **Native Anthropic SDK, no LangGraph.** The firm is a handful of ordered
   `ChatBackend` calls orchestrated by a plain async service — no LangChain/
   LangGraph (analysis §4.2). Agents exchange **concise structured reports**, not
   a growing NL chat history; natural language is reserved for the debate turns
   (the "structured-state, not telephone" discipline, analysis §4.1–4.2).

## 3. Design

Reuses the reflection loop's proven shape (`ai/reflection_service.py`,
`core/config.ReflectionConfig`, `storage/db` lesson store, `ai/agent._cycle_prompt`
injection, construction in `ApplicationKernel._wire_ai`).

### 3.1 Config — `AnalysisConfig` (`core/config.py`), nested as `ai.analysis`
```python
class AnalysisConfig(StrictModel):
    """Advisory analyst-firm → debate packet (upstream of the PM; never gates risk).

    OFF by default: it is call-heavy and only worth enabling deliberately. When
    enabled, a scheduled sweep precomputes one packet per active-watchlist symbol
    on the utility model; inject re-feeds the freshest packet into review cycles.
    """
    enabled: bool = False          # opt-in — expensive, so off by default
    inject: bool = True            # feed the freshest packet into cycle prompts
    debate_rounds: int = Field(default=2, ge=1, le=4)     # bull/bear exchanges
    risk_rounds: int = Field(default=1, ge=1, le=3)       # advisory risk-lens rounds
    refresh_hours: int = Field(default=24, ge=1)          # packet staleness bound
    max_injected: int = Field(default=3, ge=0)            # packets per cycle prompt
    max_render_chars: int = Field(default=1200, ge=200)   # hard cap per packet in the prompt
    max_symbols_per_sweep: int = Field(default=8, ge=1)   # low: a single local LM Studio
                                                          # endpoint serializes the sweep
```
> **Local-path reality:** `asyncio.gather` across the four analysts does *not*
> parallelize on one local endpoint — LM Studio serves requests serially — so a
> sweep is ~12 calls × N symbols back-to-back and can take hours if N is high,
> leaving packets perpetually stale. It fails open (no packet ⇒ PM proceeds), so
> this is a value bug, not a correctness bug — hence the low default. Raise
> `max_symbols_per_sweep` on the faster Anthropic utility path.

### 3.2 Data models (`core/models.py`), all pydantic v2, frozen where natural
- **`AnalystReport`** — `role` (fundamentals|technical|news|sentiment), `summary`
  (short structured prose), `stance` (bullish|bearish|neutral), `confidence`
  (0–1), `key_points: list[str]`, `data_gaps: list[str]`, `sources: list[str]`.
- **`DebateVerdict`** — `direction` (long|short|avoid), `conviction` (0–1),
  `bull_case: str`, `bear_case: str`, `synthesis: str`, `rounds: int`.
- **`RiskLens`** — `aggressive: str`, `neutral: str`, `conservative: str`,
  `synthesis: str`. Advisory commentary; docstring states it is not a gate.
- **`AnalysisPacket`** — `id`, `symbol`, `as_of` (UTC), `model`, `reports:
  list[AnalystReport]`, `verdict: DebateVerdict`, `risk_lens: RiskLens`,
  `snapshot_digest: str` (the pinned numbers the analysts cited), plus a
  `render()` producing the bounded block injected into the cycle prompt.

### 3.3 The firm pipeline (`ai/analysis/` package — small, single-purpose modules)
Each stage is a pure-ish async function taking a `ChatBackend` + inputs and
returning a structured model, so each is unit-testable in isolation:
- **`snapshot.py`** — `build_snapshot(router, symbol) -> Snapshot`: a deterministic
  numeric OHLCV + key-indicator snapshot from the `DataRouter` (live-data-only,
  carries `as_of`+`source`). Analysts are instructed to **cite it verbatim** rather
  than recall numbers — the anti-confabulation borrow (analysis §3.3), a free win.
- **`analysts.py`** — `run_analysts(backend, snapshot, context) -> list[AnalystReport]`:
  the four analysts run **concurrently** (`asyncio.gather`), each a single backend
  call producing a structured `AnalystReport`. The sentiment analyst reads only
  trusted news tone + snapshot price/volume momentum in v1 (no external feed); the
  external news text it consumes passes the injection scanner.
- **`debate.py`** — `run_debate(backend, reports, rounds) -> DebateVerdict`: bull
  and bear alternate for `rounds` turns (NL sub-loop over the structured reports),
  then a facilitator emits a structured `DebateVerdict`.
- **`risk_lens.py`** — `run_risk_lens(backend, verdict, reports, rounds) -> RiskLens`:
  three advisory voices + a synthesis. Explicitly advisory (invariant #2).
- **`packet.py`** — `assemble(...) -> AnalysisPacket` + the `render()` for injection.

### 3.4 Orchestration — `AnalysisService` (`ai/analysis_service.py`), mirrors `ReflectionService`
Constructor injects `db`, `router`, `config: AnalysisConfig`, `model`,
`get_backend` (returns the **utility** backend via `_wire_ai`), `watchlist`
provider, `audit_append`, and the injection scanner. Methods:
- **`run_sweep()`** — scheduled entry point. For each active-watchlist symbol (up
  to `max_symbols_per_sweep`) with no packet fresher than `refresh_hours`, spawn a
  background task → `analyze_symbol(symbol)`. Best-effort; swallows/logs errors so
  it can never break the scheduler tick.
- **`analyze_symbol(symbol)`** — snapshot → analysts → debate → risk lens →
  assemble → `db.add_analysis_packet(packet)` → one audit marker. Best-effort.
- **`relevant_packets(symbols) -> list[AnalysisPacket]`** — gated on
  `enabled and inject`; returns the freshest packet per symbol within
  `refresh_hours`, capped at `max_injected`. Wrapped in try/except → `[]`.

### 3.5 Injection + explainability (`ai/agent.py`, `app.py`)
`run_cycle(..., analysis_packets: list[AnalysisPacket] | None = None)` and
`_cycle_prompt(..., analysis_packets=...)` add an `analysis_block` to the **user
turn** (never the cached system prompt), built from each packet's `render()`. Each
`render()` is **hard-capped at `max_render_chars`** and the block is bounded by
`max_injected`, so even the max packets can't balloon the *decision-model* prompt.
Sits alongside the existing `lessons_block`; framed as advisory research the PM may
weigh or discount.

**Explainability trace (the payoff):** when a cycle is informed by packets, their
ids are recorded in the decision's metadata (**ids only** — no packet prose enters
the hash-chained audit), so "why did the PM do this" resolves back to exactly the
analysis it read. This is what makes the firm worth its cost; without the trace the
packets are invisible after the fact.

### 3.6 Wiring (`app.py`)
- In `_wire_ai(...)`: construct `self.analysis = AnalysisService(..., get_backend=
  lambda: self._utility_backend, ...)` right after the reflection service (same
  utility tier).
- In `_register_jobs`: `self.scheduler.register_job("analysis_sweep",
  self.analysis.run_sweep)`; add a default daily pre-market `ScheduleConfig` in
  `_effective_schedules` (only effective when `enabled`).
- In `run_review_cycle`: fetch `await self.analysis.relevant_packets(watchlist)`
  and pass to `agent.run_cycle(analysis_packets=...)`, exactly as lessons are.

### 3.7 Storage (`storage/db.py`)
New `analysis_packets` table (id, symbol, as_of, model, payload JSON, created_at)
with `add_analysis_packet(...)`, `recent_packets(symbols, refresh_hours, limit,
now)`, and a `packet_fresh(symbol, refresh_hours)` check for the sweep. Separate
from `trade_lessons` and from the audit chain (invariant #4).

### 3.8 Model tiering (depends on #2)
The whole firm runs on `self._utility_backend`. On the local setup that is
Devstral ($0); on the Anthropic path it is the configured `utility_model` (e.g.
Haiku), which is what makes the firm affordable (analysis §3.4/§3.5). The PM
decision and the algorithm reviewer stay on the primary — unchanged by this work.

## 4. Error handling
Fail-open and best-effort throughout, like reflection. A failed analyst degrades
to a `data_gaps` note; a failed debate/risk stage aborts just that symbol's packet
(logged, no packet stored); a retrieval error injects nothing. Nothing here raises
into the scheduler, the review cycle, or the order path. Errors subclass
`PoseidonError` where surfaced; the service swallows at its boundaries.

## 5. Testing
- **Invariant tests (the point) — assert data-flow isolation on constructed
  objects, NOT behavioral identity** (a "present vs absent" order-diff is wrong:
  the packet is *meant* to change the PM's proposal). Three checks:
  - **flow/wiring:** the `AnalysisPacket`/`RiskLens` object is handed only to
    `_cycle_prompt`'s user turn — never to `RiskEngine`, `OrderManager.
    _process_order`, the `submit_decision` schema, or the chat dispatcher (the same
    "who receives which object" assertion that caught #2's mypy-invisible swap).
  - **pinned-decision behavioral:** with the PM's returned `Decision` held fixed
    (fake backend), the risk verdict + order path are identical with/without a
    packet in context — runnable precisely *because* the decision is pinned.
  - **provenance isolation:** chat cannot read packets (like lessons).
- Each stage parses its structured output and degrades gracefully on malformed
  weak-model output (reuse the #2/local-model robustness discipline).
- `analyze_symbol` stores exactly one packet; `run_sweep` respects
  `max_symbols_per_sweep` and `refresh_hours` (no recompute when fresh).
- `relevant_packets` gates on `enabled`/`inject` and caps at `max_injected`.
- `enabled: false` ⇒ no sweep work, no injection, no schedule effect (default).
- Snapshot analysts cite pinned numbers; sentiment inputs pass the injection scan.
- Full gate: ruff / mypy --strict / pytest. `tools/ui_verify.py` not required (no
  UI). A focused adversarial review of invariants #1–#3 before release (the firm
  touches the cycle prompt, so verify the advisory isolation like #1 did).

## 6. Scope / YAGNI
- **No external social/prediction-market feed in v1** (analysis §4.4 attack
  surface). Sentiment = news tone + snapshot momentum from trusted data; the
  external news text is injection-scanned, and a hardened social connector can
  reuse that scanned path later as its own project.
- **No LangGraph/LangChain** — native SDK calls (analysis §4.2).
- **No intra-cycle / on-demand computation** — scheduled precompute only (§4.5).
- **The risk lens is advisory** — it never becomes a second risk gate (§4.1).
- **Honest framing for release notes:** it is the full-firm *structure* (4
  analysts + multi-round debate + risk lens), but **live social sentiment is
  deferred** — v1 uses a news-tone + snapshot-momentum proxy, so don't advertise
  social. Value tracks the utility model; on a weak local model the packet is
  weaker (but advisory, so the PM discounts it). The payoff is real explainability
  + the Anthropic path; do not oversell it.

## 7. Sequencing
Branch `feat/analyst-debate-packet` stacks on `feat/model-tiering` (it needs the
utility backend). Released together as **v2.10.0** (model tiering + debate packet),
per the user's "batch the release" choice. Plan will be multi-task (config →
models → storage → snapshot → analysts → debate → risk lens → packet → service →
wiring/scheduling → injection → adversarial review), TDD throughout.
