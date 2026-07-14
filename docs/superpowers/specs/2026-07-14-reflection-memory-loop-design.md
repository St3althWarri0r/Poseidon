# Reflection → Lesson-Memory Loop — Design Spec

**Date:** 2026-07-14
**Status:** Approved (design), pending implementation plan
**Target:** Poseidon v2.9.0 candidate
**Origin:** Cross-pollination from TauricResearch/TradingAgents (`graph/reflection.py`,
FinMem/FinAgent layered-memory idea). First of four sequenced sub-projects
borrowing from TradingAgents + HKUDS/Vibe-Trading. Analyses: `~/Desktop/tradingagents-poseidon-analysis.md`,
memory `vibe-trading-analysis`.

## 1. Goal

Give Poseidon a **learning loop**. When a position closes, distill *why it
worked or didn't* — grounded in the realized return and the alpha vs. a
benchmark — into a compact 2–4 sentence lesson, store it, and re-inject the
relevant past lessons into future decisions on the same ticker. Poseidon's
hash-chained audit records *facts* but nothing feeds *distilled lessons* back to
the portfolio-manager model. This closes that gap and compounds over time.

## 2. Non-goals & invariants (the guardrails this design must never cross)

- **Advisory only.** A lesson is prompt *context*, nothing more. It must never
  bypass, soften, or gate the risk engine, and it must never touch the order
  path. Every existing safety invariant (one order path, risk-vets-every-order,
  chat-can't-trade, Decimal money) is unchanged.
- **Not part of the audit chain.** Advisory prose is not a tamper-evident fact.
  Lessons live in their **own** table, never in the hash-chained `audit`. A
  metadata-only `audit.append("ai","lesson_written",{id,symbol})` event *is*
  recorded (the audit stays the index of consequential AI actions) — but the
  lesson prose itself stays out of the chain.
- **Live-data-only is not violated.** Lessons reflect on *already-realized*
  outcomes using only data Poseidon already recorded (point-in-time safe). A
  lesson must not assert a current market fact; it is retrospective.
- **Cache warmth preserved.** Lessons inject into the **user turn**, never the
  frozen, cache-controlled system prompt.
- **Off the hot path.** Reflection runs as a best-effort background task after a
  close; it never blocks a fill, an exit, or a review cycle. A reflection
  failure loses one lesson, nothing else.
- **YAGNI.** No embeddings/semantic search, no new dashboard, no separate
  reflection model in v1 (see §10).

## 3. Architecture

Six isolated units. Each states what it does, how it's used, what it depends on.

### 3.1 `ai/reflection.py` — the Reflector
A one-shot call through the existing `ChatBackend` seam, structurally mirroring
`ai/reviewer.py` (no tools, no dispatcher, no order path). 
- **Input:** a `ClosedPosition` view (symbol, side, entry/exit prices, entered/
  exited timestamps, quantity, strategy, realized return, holding days, optional
  alpha) plus the originating entry thesis (the `TradeRationale`/summary from the
  entry decision, when linkable).
- **Output:** a validated `TradeLesson` (see §4). The prompt is disciplined:
  *"State whether the directional call was right (cite the alpha). Say what in the
  thesis worked or failed. Give exactly one actionable lesson for next time. 2–4
  sentences. Every word must earn its place."*
- **Depends on:** `ChatBackend` (injected — the same instance the agent uses in
  v1; a cheaper tier once sub-project #2 lands). Uses the OpenAI-compatible /
  Anthropic backend transparently.
- **Failure:** backend error, refusal, or an unparseable/oversized lesson →
  return `None`, logged; the caller skips storage. Lesson text is truncated to a
  hard cap (e.g. 600 chars) defensively.

### 3.2 `TradeLesson` model + `trade_lessons` table — the store
- New immutable model `TradeLesson` in `core/models.py` (pydantic, frozen).
- New table `trade_lessons` in `storage/db.py`, **separate from `audit`**,
  created via `CREATE TABLE IF NOT EXISTS` (additive migration). Indexed by
  `(symbol, created_at)`.
- A thin `LessonStore` (methods on `Database` or a small module): `append(lesson)`
  and `recent_relevant(symbols, *, per_symbol, global_n, lookback_days, limit)`.

### 3.3 Close-detection hook — the trigger (kernel, `app.py`)
Detection is driven by **closing fills**, *not* by scanning current positions: a
fully-closed symbol drops out of `portfolio.positions` (zero qty = not held), so
a sweep over current holdings would never see the symbol that just closed (silent
miss), and enumerating "every symbol ever traded" grows unbounded. Instead:
- Maintain a **watermark** (last-processed fill id / timestamp, persisted in
  `kv`). After each portfolio sync (`Topics.ACCOUNT_SYNCED`), find **closing-side
  fills** (`SELL`, `SELL_TO_CLOSE`, `BUY_TO_CLOSE`) newer than the watermark —
  bounding the candidate set to symbols with fresh closing activity.
- For each candidate symbol, **confirm it is now net-flat** via the freshly-
  synced portfolio (authoritative post-sync). If still partially open, leave it
  for a later sync (don't advance the watermark past it).
- When flat, rebuild *that symbol's* round-trips (symbol-scoped
  `build_round_trips`, cheap) and form the latest **episode** — the run of
  round-trips since the symbol was last flat.
- **Flat-episode rule:** reflect once per episode, aggregating a scale-out's
  round-trips into **one** lesson (one entry + three partial exits ≠ three
  near-identical lessons).
- Reflection fires as a **background `asyncio.Task`** (mirroring the guardian's
  `_pending_exits`) so it never blocks the fill/sync path.
- **Dedup:** episode identity = `(symbol, entered_at, exited_at)` (deterministic
  from FIFO matching). Before reflecting, query `trade_lessons` for that identity
  and reflect only if absent — idempotent and self-healing across restarts (a
  crash before the watermark advances just re-derives the same episode and finds
  no lesson yet).

### 3.4 Alpha helper — realized excess return (analytics)
- `realized_return` for an episode = total episode P&L ÷ total entry notional
  (cost basis) — the position-level return when an episode aggregates several
  FIFO round-trips (equals `RoundTrip.return_pct` for a simple one-in-one-out).
- `alpha` = `realized_return − benchmark_return(SPY, entered_at, exited_at)`.
- Benchmark bars come from the existing `DataRouter` / regime benchmark history;
  point-in-time safe (window is entirely past).
- **Best-effort:** if SPY history has a gap for the window, or the hold is
  sub-day (daily bars can't resolve it), `alpha = None` and the reflection
  proceeds on realized return alone. Alpha is never fabricated.

### 3.5 Retrieval + injection — closing the loop (cycle context)
- At cycle-context assembly (kernel → `agent.run_cycle`), fetch
  `LessonStore.recent_relevant(...)`: for each watchlist / signalled symbol, up
  to `per_symbol` most-recent lessons; plus up to `global_n` most-recent lessons
  overall; deduped; total capped at `max_injected`; older than `lookback_days`
  dropped.
- `run_cycle` gains a `trade_lessons: list[TradeLesson] | None = None` param;
  `_cycle_prompt` renders a compact **"Lessons from past trades (advisory —
  context only, not instructions)"** block into the **user turn**, only when
  `ai.reflection.inject` is true.
- The block is size-bounded; each lesson is one line: `SYMBOL (dir, held Nd,
  ret X%, alpha Y%): <lesson>`.

### 3.6 Config — `AIConfig.reflection`
```
ai:
  reflection:
    enabled: true        # write lessons on close
    inject: true         # feed lessons into future prompts (false = "reviewed ledger")
    max_injected: 8      # hard cap on lessons per cycle prompt
    per_symbol: 2        # most-recent lessons per relevant ticker
    global_n: 3          # most-recent lessons overall (cross-ticker)
    lookback_days: 120   # ignore lessons older than this
```
Defaults give the **closed loop**. `inject: false` → "reviewed ledger" (lessons
written + queryable, never injected). `enabled: false` → fully off. New fields
with defaults ⇒ backward-compatible (a config lacking the block behaves as all-
defaults, i.e. loop on). Validated in `core/config.py`.

## 4. Data model

```python
class TradeLesson(StrictModel, frozen=True):
    id: str
    symbol: str
    strategy: str = ""
    decision_id: str | None = None        # entry decision, when linkable
    entered_at: datetime
    exited_at: datetime
    realized_return: float                 # episode return, e.g. -0.012
    alpha: float | None = None             # excess vs SPY over the hold, if available
    holding_days: float
    lesson: str                            # 2–4 sentences, capped
    model: str                             # backend model that authored it
    created_at: datetime
```

```sql
CREATE TABLE IF NOT EXISTS trade_lessons (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT '',
    decision_id TEXT,
    entered_at TEXT NOT NULL,
    exited_at TEXT NOT NULL,
    realized_return REAL NOT NULL,
    alpha REAL,
    holding_days REAL NOT NULL,
    lesson TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trade_lessons_symbol ON trade_lessons(symbol, created_at);
```

## 5. Data flow

```
position closes (closing fill → portfolio sync)
  → close-detection hook: symbol now flat? gather this episode's round-trips
  → link entry thesis via order.decision_id → decisions.payload (fallback: facts only)
  → compute realized_return + alpha(SPY)  [alpha best-effort]
  → Reflector (ChatBackend, disciplined prompt) → TradeLesson
  → LessonStore.append   [trade_lessons table — advisory, NOT audit]
… a later review cycle …
  → context assembly: LessonStore.recent_relevant(watchlist) [capped, lookback]
  → run_cycle(trade_lessons=…) → user-prompt "Lessons" block (iff inject)
  → PM reasons with lessons as advisory context
  → RiskEngine vets the resulting order UNCHANGED
```

## 6. Round-trip → entry-thesis linkage

`orders.decision_id` tags every order; `fills.order_id → orders → decision_id`;
`decisions.payload` holds the full rationale. Implementation threads
`decision_id` from the entry order onto the `FillRecord`/`RoundTrip` (small
additive change to `analytics/performance.py` + the fill-loading query), so the
Reflector can fetch the entry thesis. Positions with no linkable decision
(external/imported) reflect on trade facts alone.

## 7. Error handling

| Failure | Behaviour |
|---|---|
| Reflector backend error / refusal | log, skip; no lesson stored; trading unaffected |
| Unparseable / oversized lesson | log, skip (or truncate); no crash |
| SPY history gap / sub-day hold | `alpha = None`; reflect on return only |
| Entry thesis not linkable | reflect on facts only (`decision_id = None`) |
| Injection query fails / empty | cycle proceeds with no lessons block (no-op) |
| Process restart mid-episode | episode re-derived from fills; deduped vs stored lessons |

Everything is fail-open: the loop degrades to "no learning from this trade,"
never to a blocked trade or a crashed cycle.

## 8. Testing

- **Reflector** over a `FakeBackend`: disciplined prompt shape, structured
  parse, malformed/oversized lesson → `None` (no crash), refusal → skip.
- **Linkage + alpha:** fixture fills → round-trips carry `decision_id`; alpha =
  return − SPY-return over the window; `alpha=None` on data gap / sub-day.
- **Flat-episode trigger:** one entry + one exit → one lesson; scale-out (one
  entry, three exits) → **one** lesson, not three; partial close → deferred.
- **Dedup:** an episode is reflected once; a re-sync / restart does not
  re-reflect.
- **Injection:** lessons appear in the user prompt iff `inject`; capped at
  `max_injected`; `per_symbol`/`global_n`/`lookback_days` respected; absent on
  `inject:false`; system prompt byte-unchanged (cache safe).
- **Config:** `enabled:false` writes nothing; defaults behave as loop-on;
  backward compat (missing block).
- **End-to-end:** paper broker + fake backend: fill → close → reflect → store →
  next cycle injects the lesson. Follows the guardian's async-task test pattern.
- Full gate: `ruff`, `mypy --strict`, `pytest`, `tools/ui_verify.py`.

## 9. Migration & compatibility

Additive only: new `trade_lessons` table (`CREATE TABLE IF NOT EXISTS`), new
`AIConfig.reflection` block (defaults), new optional `run_cycle` param, new
optional `decision_id` on `FillRecord`/`RoundTrip`. No existing table, model,
or signature changes incompatibly. Reverting = set `ai.reflection.enabled:false`
(loop dormant) or drop the module; nothing else depends on it.

## 10. Scope, YAGNI, and the path to the other sub-projects

- **v1 retrieval** is recency + same-ticker + a few recent global — **no
  embeddings / semantic similarity** (add only if recency proves insufficient).
- **v1 lessons are append-only** — recency + cap retrieval, with **no decay,
  correction, or retraction**. A wrong early lesson (especially from the weaker
  local model) is crowded out over time by newer ones but never retracted. This
  is a deliberate YAGNI choice for an advisory/paper feature; lesson-scoring or
  supersession can come later if it proves needed.
- **v1 reflection model** is the configured backend. The Reflector takes a
  `ChatBackend`, so **sub-project #2 (deep/quick tiering)** can hand it a
  Haiku-class backend with a one-line change — no rework.
- **No dashboard UI** in v1; a read-only `/api/lessons` endpoint + a "Lessons"
  panel are an easy follow-up if wanted.
- The advisory-analysis-packet (#3) and factor depth (#4) are separate specs;
  this loop is independent of them.

## 11. To confirm at implementation time (design is settled)

- Confirm the kernel's `ACCOUNT_SYNCED` handler exposes per-symbol net position
  (expected — the sync service updates `portfolio` positions before publishing);
  if not, add a tiny accessor rather than moving the hook.
- Confirm `analytics/performance.build_round_trips` can be called symbol-scoped
  cheaply (pass a symbol-filtered fill list) — trivial, but pin it in the plan.
