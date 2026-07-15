# Strategy-Decay Tracking — Design Spec

**Date:** 2026-07-15
**Status:** Approved (design), pending implementation plan
**Target:** Poseidon v2.12.0 candidate
**Origin:** Sub-project **#4b** of the cross-pollination program ([[poseidon-crosspollination-program]]),
the final piece — from Vibe-Trading's strategy-decay lifecycle ([[vibe-trading-analysis]]).
Reuses #4a's rolling-significance discipline and #1's per-strategy closed-trade reconstruction.

## 1. Goal

Watch each trading strategy's **rolling realized performance** and flag when its edge has
**decayed**, so a stale screener is retired instead of quietly losing money. A per-strategy
lifecycle state machine (`healthy → watch → decaying → retire_recommended`, with recovery) is
driven by the strategy's recent closed trades and surfaced to the operator. It is **advisory by
default**; an opt-in mode auto-retires a decayed *custom* strategy.

## 2. Invariants — the safety properties

1. **It cannot place, size, or approve a trade — it can only ever REDUCE trading.** The one
   action it can take (opt-in) is *deactivating a strategy* in the workshop, which stops that
   screener from emitting signals. It never calls the risk engine, the order path,
   `submit_decision`, or a broker. Fail-safe direction: retiring a strategy can only remove
   trades, never add one. **Exit-coverage guarantee (VERIFY before enabling `auto_retire`):**
   deactivating a strategy must NOT orphan the stop-loss/take-profit management of positions it
   already opened — the `PositionGuardian`/`exit_plans` manage exits per-position, independent of
   whether the originating screener is still active. Confirmed against the code before documenting
   `auto_retire` as safe; if exit coverage were keyed off strategy-active state, auto-retire would
   strand live risk (the opposite of reduce-only) and must stay disabled.
2. **Advisory by default.** `enabled` defaults on (it only flags + audits), but `auto_retire`
   defaults **off** — with it off, the service only writes health state, audits transitions, and
   notifies; retiring is the operator's decision.
3. **Auto-retire is scoped, audited, reversible.** When enabled, it only deactivates *custom
   workshop* strategies (builtin strategies are flag-only — they have no deactivation hook), it
   audits the action as the `system` actor (never `human`), and `deactivate(archive=False)`
   leaves the strategy as a reactivatable draft.
4. **Conservative on thin data.** Strategies fire few trades, so a decline is only ever escalated
   with `>= min_trades` in the window AND a statistically-meaningful drop AND a sustained streak
   (hysteresis). Insufficient data → `watch`/hold, never `decaying`. The report labels
   low-trade-count assessments as low-confidence (the analog of #4a's thin-universe caveat).
5. **Off the hot path, best-effort.** It runs on a scheduled sweep, reads already-recorded fills,
   and any failure logs and is swallowed — it never blocks a cycle, a fill, or an exit.

## 3. Design

Reuses `analytics/performance.py` (`RoundTrip`, `build_round_trips`), the fill loader in
`app.py`, `AlgorithmWorkshop` (status lifecycle), `NotificationService`, the `Scheduler`, and the
service/config/storage patterns from #1/#3.

### 3.1 Config — `StrategyHealthConfig` (`core/config.py`, nested `strategy_health`)
```python
class StrategyHealthConfig(StrictModel):
    enabled: bool = True            # flag-only monitoring; advisory
    auto_retire: bool = False       # opt-in: deactivate a decayed CUSTOM strategy
    window_trades: int = 20         # trailing window (most-recent closed trades)
    min_trades: int = 8             # below this the window is "insufficient" (never decaying)
    baseline_min_trades: int = 20   # baseline needs at least this many prior trades
    decay_t: float = 2.0            # t-stat threshold (DYING: window edge sig. <= 0)
    decay_streak: int = 2           # consecutive DYING sweeps: watch -> decaying
    retire_streak: int = 4          # consecutive DYING sweeps: -> retire_recommended
    recover_streak: int = 2         # consecutive OK sweeps to step back toward healthy
```

### 3.2 State machine (`analytics/decay.py`) — pure, testable
- **`HealthState`** enum: `HEALTHY, WATCH, DECAYING, RETIRE_RECOMMENDED` (string enum).
- **`assess(trips, cfg) -> Assessment`** (pure): sort a strategy's `RoundTrip`s by `exited_at`;
  `window = trips[-window_trades:]`, `baseline = trips[:-window_trades]` (disjoint). Compute
  window mean `return_pct` + **sample** stdev; baseline mean. Emit a signal — **only a genuinely
  unprofitable edge escalates toward retirement; a merely lower-but-still-positive edge does not**
  (a strategy that mean-reverts from +2% to a still-good +0.5% is normalizing, not dying, and
  must not be auto-killed):
  - `INSUFFICIENT` if `len(window) < min_trades` or `len(baseline) < baseline_min_trades`.
  - `DYING` — the honest decay signal — if the window edge is significantly `<= 0` by a
    **one-sample** t-test: `t0 = win_mean / (win_std/sqrt(n))` and `t0 <= −decay_t`. (Guard
    `win_std == 0`: `DYING` iff `win_mean < 0`.) This is the ONLY signal that escalates to
    `decaying`/`retire_recommended`.
  - `SOFTENING` if the edge is still positive but materially below baseline (`win_mean > 0` and
    `win_mean < base_mean − decay_t*win_std/sqrt(n)`). Informational: caps the state at `watch`,
    never beyond.
  - `OK` otherwise. `Assessment` carries the signal + metrics (win_mean, base_mean, t0, n,
    win_rate) for the report/audit.
- **`advance(state, decline_streak, recover_streak, signal, cfg) -> (state, decline_streak,
  recover_streak)`** (pure): the hysteresis transition. Only `DYING` escalates:
  `DYING` increments `decline_streak` (resets recover) and escalates `HEALTHY→WATCH→
  (decline_streak≥decay_streak)DECAYING→(decline_streak≥retire_streak)RETIRE_RECOMMENDED`.
  `SOFTENING` resets `decline_streak` (it is NOT dying), bumps `HEALTHY→WATCH`, and never escalates
  beyond `watch` or recovers. `OK` increments `recover_streak` (resets decline) and steps the state
  back one rung (`RETIRE_RECOMMENDED→DECAYING→WATCH→HEALTHY`) after `recover_streak≥cfg.recover_streak`.
  `INSUFFICIENT` holds the state and both counters. Every real state change is the caller's cue to
  audit + (maybe) notify.

### 3.3 Models + storage
- **`core/models.StrategyHealth`**: `strategy`, `state: str`, `decline_streak: int`,
  `recover_streak: int`, `window_return: float`, `baseline_return: float`, `t_stat: float`,
  `trades: int`, `updated_at: datetime`.
- **`storage/db`**: `strategy_health` table (`strategy` PK, `state`, `streak`, `payload` JSON,
  `updated_at`); `upsert_strategy_health`, `get_strategy_health(strategy)`,
  `list_strategy_health()`. Separate from the audit chain (health is derived, not a fact) — but a
  one-line `audit.append("system", "strategy.health_changed", {...})` marks each transition.

### 3.4 Service — `StrategyHealthService` (`analytics/decay_service.py`), mirrors ReflectionService
Constructor injects `db`, `config`, `load_trips: Callable[[], Awaitable[list[RoundTrip]]]`
(reuses the app's per-strategy fill→round-trip loader), `audit_append`, `notify:
Callable[[str, dict], Awaitable[None]]` (advisory notification), and `retire:
Callable[[str], Awaitable[bool]]` (deactivate a custom strategy by name → True if it did;
False/None for builtin or missing → flag-only). Methods:
- **`sweep()`** — scheduled. Load all round-trips, group by `strategy`; for each strategy: load
  prior `StrategyHealth`, `assess`, `advance`, and if the state changed: persist, audit, notify
  on a downgrade (to `DECAYING`/`RETIRE_RECOMMENDED`), and if `auto_retire` and new state is
  `RETIRE_RECOMMENDED`, call `retire(strategy)` (audited `system`; only acts on custom
  strategies). Best-effort; swallows/logs per-strategy errors so one bad strategy can't break the
  sweep.
- **`report() -> list[StrategyHealth]`** — read `list_strategy_health()` for surfacing.

### 3.5 Wiring (`app.py`)
Construct `self.strategy_health = StrategyHealthService(...)` in `start()` after the workshop +
notifier + sync exist; pass `load_trips` (the existing fill→round-trip loader, e.g. behind
`_load_strategy_trips`), `notify` (via `NotificationService`), and `retire` (a small adapter that
maps a strategy *name* → an active custom algorithm id via `workshop.list_all()` and calls
`workshop.deactivate(id, archive=False)` with a `system`-actor audit; returns False for builtin/
unknown). Register a scheduled job `strategy_health_sweep` (default daily, only effective when
`enabled`, added in `_effective_schedules` like the analysis sweep). Expose `report()` for a
future dashboard tile (surfacing beyond audit+notify is a later step).

## 4. Error handling
Best-effort throughout, like reflection: a per-strategy assess/persist/retire failure logs and is
skipped; the sweep never raises into the scheduler. `retire` failures (e.g. a race with the
operator) are logged and leave the recommendation standing. Nothing here is on a live path.

## 5. Testing
- **The safety invariant:** the service is constructed with a `retire` callable and asserts (a) it
  is NEVER invoked when `auto_retire=False`, and (b) when invoked it only ever *deactivates* — the
  test's fake `retire`/workshop can only reduce active strategies, never activate/order. Assert the
  service has no reference to the risk engine / order manager / broker (constructor + wiring).
- **Decay ≠ normalization (with real numbers, not "a dip"):** a window that is *lower than
  baseline but still clearly profitable* (e.g. baseline +2%/trade, window +0.5% tight) must return
  `SOFTENING` and never escalate past `watch`; a window whose edge is *genuinely negative and
  significant* (e.g. −1.5%/trade, tight) must return `DYING`. Assert both cross/don't-cross the
  Prong-B threshold numerically.
- **Hysteresis (pure):** a SINGLE `DYING` sweep never reaches `decaying` (needs `decay_streak`);
  `RETIRE_RECOMMENDED` needs `retire_streak`; `INSUFFICIENT` holds state + both counters; `OK`
  recovers one rung only after `recover_streak`.
- **Conservative on noise:** a strategy with a big but *low-n* dip returns `INSUFFICIENT` (stays
  ≤ `watch`), never `decaying`.
- Storage round-trips `StrategyHealth`; `sweep` audits exactly on transitions (not every sweep);
  a downgrade notifies, a recovery does not; `auto_retire` calls `retire` only on
  `RETIRE_RECOMMENDED` and only for custom strategies (builtin → flag-only).
- Full gate (ruff / mypy --strict / pytest). No UI. A final review should scrutinize the safety
  invariant (reduce-only, advisory-default) and the hysteresis correctness.

## 6. Scope / YAGNI
- **Rolling realized performance**, not a cross-sectional factor IC (strategies emit discrete
  signals, not continuous scores) — the natural decay metric. It borrows #4a's rolling
  sample-stdev / t-stat *significance* discipline, not its cross-sectional harness.
- **No dashboard UI in v1** — audit + notification + a `report()` method; a health tile is a
  later step.
- **No auto-*re-activation*** — recovery walks the state back toward healthy and clears the
  recommendation, but re-enabling a strategy is always the operator's call.
- **Honest framing:** decay detection on few trades is inherently low-power; the feature is a
  conservative early-warning + bookkeeping aid, not a precise timing signal — the report says so.

## 7. Sequencing
Branch `feat/strategy-decay-tracking` off current `main` (v2.11.0). Its own release **v2.12.0** —
the final release of the four-part cross-pollination program.
