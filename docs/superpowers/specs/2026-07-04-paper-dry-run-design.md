# Paper Dry Run — design

**Date:** 2026-07-04
**Status:** Approved (design) — pending implementation
**Version target:** 2.5.0 (new feature)

## Goal

Give the operator a **risk-free, real-time dry run** of the full autonomous
stack — Claude *and* the built-in algorithms trading the **paper account** —
so they can watch the whole system work and build confidence before going
live. This is a *discoverability + framing* feature: the underlying capability
already exists (paper broker + autonomous mode + activatable algorithms + a
scheduled/on-demand review cycle). Nothing about fills, the risk engine, or
execution semantics changes.

## Non-goals (YAGNI)

- No accelerated / historical / time-compressed simulation (the backtest
  engine already covers algorithm replay; Claude-in-the-loop replay is out of
  scope).
- No change to how the paper broker fills, or to the risk engine's real-time
  quote requirement. Autonomous paper trades still only execute during market
  hours — that is correct and realistic; the panel just makes it visible.
- No new order/fill/risk code paths.

## Approach

A dedicated, **transparent guided panel** (a new "Dry Run" left-nav view) whose
controls call the *existing, already-tested* endpoints. Only one new
**read-only** endpoint is added to aggregate state. Chosen over a server-side
"start/stop orchestrator" because the operator asked to *see and control each
piece*, and over a "just fix framing" approach because they asked for a guided
panel.

## User experience

A new **"Dry Run"** item in the left nav opens a panel with:

1. **Safe-simulation banner** (persistent, top): "PAPER — no real money. This is
   a safe simulation."
2. **Three status rows**, each showing current state and a toggle, turning green
   when satisfied:
   - **Broker = Paper simulator.** Toggle switches the active broker to `paper`.
     If a *live* broker is active it explains it will switch you to paper first;
     it never flips a live broker into the dry run.
   - **Built-in algorithms active.** Lists the bundled starter algorithms with
     their active/draft status; a "Activate the built-in algorithms" action
     flips the draft starters to active. Individual activate toggles too.
   - **Autonomous mode.** Toggle sets Autonomous. Because the broker is paper,
     no "real money" confirmation is shown.
3. **Market indicator:** "Market open — trades can execute" or "Market closed —
   the dry run will start trading at the next open (9:30 ET)."
4. **"Run a review cycle now"** button — triggers Claude on demand instead of
   waiting for the scheduler.
5. **When all three rows are green:** "✅ Dry run active — Claude and your
   algorithms are trading the paper account," plus a **"Stop dry run"** button
   that returns the platform to **Research** mode (safe default; leaves broker
   and algorithms as-is so the run can be resumed).

## Components

### 1. `GET /api/dryrun` (new, read-only) — `api/server.py`

Aggregates the panel's state in one call so the client does not orchestrate
several reads. Returns:

```json
{
  "broker_is_paper": true,
  "active_broker": "paper",
  "mode": "research",
  "algorithms": [{"id": "...", "name": "...", "status": "draft", "bundled": true}],
  "active_algo_count": 0,
  "bundled_draft_count": 2,
  "market": {"session": "closed", "is_open": false, "opens_hint": "9:30 ET"}
}
```

- `broker_is_paper` / `active_broker` from `kernel.broker`.
- `mode` from `kernel.order_manager.mode`.
- `algorithms` from `kernel.workshop.list_all()`, trimmed to `id/name/status`
  plus a `bundled` flag derived from the seed marker
  (`review_notes == "bundled example — review before activating"`). Centralizing
  the (slightly fragile) marker here keeps the UI simple; if it proves fragile,
  a follow-up can add a first-class `origin` column — out of scope now.
- `market` from `kernel.clock.session()` (an existing method returning a
  `MarketSession`); `is_open` ⇔ `session() == MarketSession.REGULAR`, and
  `opens_hint` is a static "9:30 ET" string when not open.

Depends on: `kernel.broker`, `kernel.order_manager`, `kernel.workshop`,
`kernel.clock`. No writes.

### 2. "Dry Run" view — `api/static/{index.html, app.js, style.css}`

- New nav item + view container.
- `renderDryRun(state)` renders banner, the three rows, market indicator,
  run-now, stop, and the active/stop summary from `GET /api/dryrun`.
- Toggle actions **reuse existing endpoints**:
  - Broker→paper: `POST /api/brokers/connect {name:"paper", paper:true}`.
  - Activate a starter: `POST /api/algorithms/{id}/activate` (looped for
    "activate all starters", over the `bundled && status==draft` entries).
  - Autonomous / Stop: `POST /api/mode {mode:"autonomous"|"research"}`.
  - Run now: `POST /api/cycle`.
- After any action, re-fetch `GET /api/dryrun` and re-render. The view also
  refreshes on websocket status/order events (existing hub).
- Failures use the existing toast pattern.

### 3. Framing bug fix — `api/static/app.js`

The header autonomous-mode confirm and live-warning only warn about "real
money" when the active broker is **live** (`broker.paper === false`). On paper
they are silent/reassuring. (Fixes a misleading warning introduced in 2.4.0.)

## Safety invariants

- The Dry Run panel only ever sets Autonomous while the active broker is
  **paper**. A live broker is never flipped into autonomous from this panel —
  the header mode control (with its real-money confirm) remains the only path to
  live autonomous.
- **Stop dry run → Research mode**, which halts all order execution — the safe
  default.
- Purely orchestration + presentation over existing capabilities; no change to
  fills, risk checks, or execution.

## Error handling

- Each toggle surfaces failures via toast (existing pattern).
- Switching to paper while a live broker has open orders reuses the existing
  broker-switch drain guard and its error message.
- Market closed: shown explicitly; "Run now" still runs a cycle (Claude
  analyzes; trades are risk-rejected on stale quotes — expected, and the panel
  says so).

## Testing

- **Unit:** `GET /api/dryrun` aggregation reflects broker/mode/algorithms/market
  from a stub kernel (mirrors the `FakeKernel` used by the UI harness).
- **Browser (`tools/ui_verify.py`):** add a Dry Run walkthrough — nav to the
  view; assert the safe banner, three rows, and market indicator render; flip
  the autonomous toggle and assert state; click "Run now"; click "Stop dry run"
  and assert mode returns to research. Extend `FakeKernel` with the `/api/dryrun`
  data.

## Rollout

- Version bump to **2.5.0** (new feature), README/docs/test-count updates.
- Docs: a short "Paper dry run" section in `docs/user-guide.md`.
