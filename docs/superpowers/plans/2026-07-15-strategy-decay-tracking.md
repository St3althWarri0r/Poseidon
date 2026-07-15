# Strategy-Decay Tracking (#4b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `StrategyHealthService` that watches each strategy's rolling realized performance and drives a per-strategy lifecycle state machine (`healthy → watch → decaying → retire_recommended`, with recovery + hysteresis). Advisory by default; opt-in auto-retire deactivates a decayed custom strategy. It can only ever REDUCE trading — never the risk/order path.

**Architecture:** A pure state machine (`analytics/decay.py`: `assess` + `advance`) over the existing `RoundTrip` data, plus a best-effort scheduled `StrategyHealthService` (`analytics/decay_service.py`) that persists health, audits transitions, notifies on downgrades, and optionally retires. Mirrors the reflection service. Spec: `docs/superpowers/specs/2026-07-15-strategy-decay-tracking-design.md`.

**Tech Stack:** stdlib `statistics`/`math`/`enum`, pydantic (`StrategyHealth`), `RoundTrip` (analytics/performance), `AlgorithmWorkshop`, `Scheduler`, pytest.

## Global Constraints

- Python 3.11+, `from __future__ import annotations`, mypy `--strict`, ruff line length 100.
- **Reduce-only safety:** the service never imports/calls `RiskEngine`, `OrderManager`, a broker, or `submit_decision`. Its only mutating action (opt-in) is `retire(strategy)` = deactivate a custom strategy. `auto_retire` defaults **False**.
- **Decay ≠ normalization:** only a genuinely-unprofitable edge (`DYING`: window edge significantly `<= 0`) escalates to `decaying`/`retire_recommended`. A lower-but-still-positive edge is `SOFTENING` → caps at `watch`.
- **Conservative on thin data:** `< min_trades` or `< baseline_min_trades` ⇒ `INSUFFICIENT`, never `decaying`. Hysteresis: escalation needs a streak.
- **Best-effort:** the sweep swallows/logs per-strategy errors and never raises into the scheduler.
- Stdlib stats only (`statistics.fmean`/`stdev`, `math.sqrt`). `RoundTrip` has `.strategy`, `.return_pct` (float), `.exited_at` (datetime), `.pnl` (Decimal).
- Gate: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest`. No UI.

---

### Task 1: `StrategyHealthConfig`

**Files:**
- Modify: `src/poseidon/core/config.py` (add `StrategyHealthConfig`; add `strategy_health` to `AppConfig`)
- Test: `tests/unit/test_strategy_health_config.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_strategy_health_config.py
from __future__ import annotations

from poseidon.core.config import AppConfig, StrategyHealthConfig


def test_defaults() -> None:
    c = AppConfig().strategy_health
    assert isinstance(c, StrategyHealthConfig)
    assert c.enabled is True and c.auto_retire is False       # advisory by default
    assert c.window_trades == 20 and c.min_trades == 8
    assert c.baseline_min_trades == 20 and c.decay_t == 2.0
    assert c.decay_streak == 2 and c.retire_streak == 4 and c.recover_streak == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_strategy_health_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'StrategyHealthConfig'`.

- [ ] **Step 3: Write minimal implementation**

In `core/config.py`, near `ReflectionConfig`:
```python
class StrategyHealthConfig(StrictModel):
    """Strategy-decay watchdog (advisory). Flags a strategy whose realized edge has
    decayed to <= 0; opt-in auto_retire deactivates a decayed CUSTOM strategy. It can
    only reduce trading — it never touches the risk engine or the order path."""

    enabled: bool = True
    auto_retire: bool = False
    window_trades: int = Field(default=20, ge=1)
    min_trades: int = Field(default=8, ge=1)
    baseline_min_trades: int = Field(default=20, ge=1)
    decay_t: float = Field(default=2.0, gt=0)
    decay_streak: int = Field(default=2, ge=1)
    retire_streak: int = Field(default=4, ge=1)
    recover_streak: int = Field(default=2, ge=1)
```
Add to `AppConfig`: `strategy_health: StrategyHealthConfig = Field(default_factory=StrategyHealthConfig)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_strategy_health_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/core/config.py tests/unit/test_strategy_health_config.py
git commit -m "feat(config): add strategy_health block for the decay watchdog"
```

---

### Task 2: `assess` — the pure decay assessment (`analytics/decay.py`)

**Files:**
- Create: `src/poseidon/analytics/decay.py`
- Test: `tests/unit/test_decay_assess.py`

**Interfaces:**
- Consumes: `RoundTrip` (analytics/performance), `StrategyHealthConfig`.
- Produces: `HealthState` + `Signal` enums; `Assessment` dataclass; `assess(trips, cfg) -> Assessment`.

- [ ] **Step 1: Write the failing test** (real numbers — decay vs normalization)
```python
# tests/unit/test_decay_assess.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.analytics.performance import RoundTrip
from poseidon.analytics.decay import Signal, assess
from poseidon.core.config import StrategyHealthConfig


def _trip(r: float, day: int) -> RoundTrip:
    entry = Decimal("100")
    return RoundTrip(symbol="X", strategy="s", quantity=Decimal("1"), entry_price=entry,
                     exit_price=entry * Decimal(str(1 + r)),
                     entered_at=datetime(2024, 1, 1, tzinfo=UTC),
                     exited_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day))


def _cfg(**kw) -> StrategyHealthConfig:
    return StrategyHealthConfig(**kw)


def _series(rets: list[float]) -> list[RoundTrip]:
    return [_trip(r, d) for d, r in enumerate(rets)]


def test_insufficient_under_min_trades() -> None:
    trips = _series([0.01] * 25 + [-0.01] * 3)          # window only 3 (< min_trades 8)
    assert assess(trips, _cfg(window_trades=3, min_trades=8)).signal is Signal.INSUFFICIENT


def test_dying_when_edge_significantly_negative() -> None:
    # baseline +2%, recent window tightly around -1.5% -> t0 << -2 -> DYING
    trips = _series([0.02] * 25 + [-0.015, -0.014, -0.016, -0.015, -0.013, -0.017, -0.015, -0.014,
                                   -0.016, -0.015])
    a = assess(trips, _cfg(window_trades=10, min_trades=8, baseline_min_trades=20))
    assert a.signal is Signal.DYING and a.window_return < 0


def test_softening_not_dying_when_lower_but_positive() -> None:
    # baseline +2%, window tightly around +0.5% -> still profitable -> SOFTENING (NOT dying)
    trips = _series([0.02] * 25 + [0.005, 0.004, 0.006, 0.005, 0.005, 0.004, 0.006, 0.005,
                                   0.005, 0.006])
    a = assess(trips, _cfg(window_trades=10, min_trades=8, baseline_min_trades=20))
    assert a.signal is Signal.SOFTENING           # normalization, not death
    assert a.window_return > 0


def test_ok_when_in_line_with_baseline() -> None:
    trips = _series([0.02, 0.018, 0.022, 0.019, 0.021] * 8)   # steady ~+2%
    assert assess(trips, _cfg(window_trades=10)).signal is Signal.OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_decay_assess.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'poseidon.analytics.decay'`.

- [ ] **Step 3: Write minimal implementation**

`src/poseidon/analytics/decay.py`:
```python
"""Strategy-decay assessment + lifecycle state machine. Pure — no I/O. Only a
genuinely-unprofitable edge (DYING) escalates toward retirement; a lower-but-still-
positive edge is SOFTENING (normalization, not death) and caps at WATCH."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import Enum

from ..core.config import StrategyHealthConfig
from .performance import RoundTrip


class HealthState(str, Enum):
    HEALTHY = "healthy"
    WATCH = "watch"
    DECAYING = "decaying"
    RETIRE_RECOMMENDED = "retire_recommended"


class Signal(str, Enum):
    INSUFFICIENT = "insufficient"
    OK = "ok"
    SOFTENING = "softening"
    DYING = "dying"


@dataclass(frozen=True)
class Assessment:
    signal: Signal
    window_return: float
    baseline_return: float
    t0: float
    trades: int
    win_rate: float


def assess(trips: list[RoundTrip], cfg: StrategyHealthConfig) -> Assessment:
    ordered = sorted(trips, key=lambda t: t.exited_at)
    window = ordered[-cfg.window_trades:]
    baseline = ordered[:-cfg.window_trades]
    n = len(window)
    wr = [t.return_pct for t in window]
    win_mean = statistics.fmean(wr) if wr else 0.0
    win_rate = (sum(1 for t in window if t.pnl > 0) / n) if n else 0.0
    base_mean = statistics.fmean([t.return_pct for t in baseline]) if baseline else 0.0
    if n < cfg.min_trades or len(baseline) < cfg.baseline_min_trades:
        return Assessment(Signal.INSUFFICIENT, win_mean, base_mean, 0.0, n, win_rate)
    win_std = statistics.stdev(wr) if n >= 2 else 0.0
    if win_std == 0.0:                       # degenerate all-equal window: no t-stat
        sig = (Signal.DYING if win_mean < 0
               else Signal.SOFTENING if win_mean < base_mean else Signal.OK)
        return Assessment(sig, win_mean, base_mean, 0.0, n, win_rate)
    se = win_std / math.sqrt(n)
    t0 = win_mean / se                       # one-sample t-test vs 0
    if t0 <= -cfg.decay_t:
        return Assessment(Signal.DYING, win_mean, base_mean, t0, n, win_rate)
    if win_mean > 0 and win_mean < base_mean - cfg.decay_t * se:
        return Assessment(Signal.SOFTENING, win_mean, base_mean, t0, n, win_rate)
    return Assessment(Signal.OK, win_mean, base_mean, t0, n, win_rate)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_decay_assess.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/analytics/decay.py tests/unit/test_decay_assess.py
git commit -m "feat(analytics): pure strategy-decay assessment (dying vs softening)"
```

---

### Task 3: `advance` — the hysteresis state machine (`analytics/decay.py`)

**Files:**
- Modify: `src/poseidon/analytics/decay.py` (add `advance`)
- Test: `tests/unit/test_decay_advance.py`

**Interfaces:**
- Produces: `advance(state, decline_streak, recover_streak, signal, cfg) -> tuple[HealthState, int, int]`.

- [ ] **Step 1: Write the failing test** (hysteresis: a single DYING never reaches decaying)
```python
# tests/unit/test_decay_advance.py
from __future__ import annotations

from poseidon.analytics.decay import HealthState, Signal, advance
from poseidon.core.config import StrategyHealthConfig


def _cfg(**kw) -> StrategyHealthConfig:
    return StrategyHealthConfig(**kw)


def test_single_dying_only_reaches_watch() -> None:
    cfg = _cfg(decay_streak=2, retire_streak=4)
    state, d, r = advance(HealthState.HEALTHY, 0, 0, Signal.DYING, cfg)
    assert state is HealthState.WATCH and d == 1        # NOT decaying on one sweep


def test_streak_escalates_to_decaying_then_retire() -> None:
    cfg = _cfg(decay_streak=2, retire_streak=4)
    state, d, r = HealthState.HEALTHY, 0, 0
    seen = []
    for _ in range(5):
        state, d, r = advance(state, d, r, Signal.DYING, cfg)
        seen.append(state)
    assert seen[0] is HealthState.WATCH                 # streak 1
    assert seen[1] is HealthState.DECAYING              # streak 2 == decay_streak
    assert seen[3] is HealthState.RETIRE_RECOMMENDED    # streak 4 == retire_streak


def test_softening_caps_at_watch_and_resets_decline() -> None:
    cfg = _cfg()
    state, d, r = advance(HealthState.HEALTHY, 3, 0, Signal.SOFTENING, cfg)
    assert state is HealthState.WATCH and d == 0        # not dying -> no escalation


def test_insufficient_holds_state_and_counters() -> None:
    cfg = _cfg()
    assert advance(HealthState.DECAYING, 3, 0, Signal.INSUFFICIENT, cfg) == (
        HealthState.DECAYING, 3, 0)


def test_ok_recovers_one_rung_after_recover_streak() -> None:
    cfg = _cfg(recover_streak=2)
    state, d, r = advance(HealthState.DECAYING, 0, 0, Signal.OK, cfg)
    assert state is HealthState.DECAYING and r == 1     # not yet
    state, d, r = advance(state, d, r, Signal.OK, cfg)
    assert state is HealthState.WATCH and r == 0        # stepped down one rung
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_decay_advance.py -q`
Expected: FAIL — `ImportError: cannot import name 'advance'`.

- [ ] **Step 3: Write minimal implementation**

Append to `analytics/decay.py`:
```python
_LADDER = [HealthState.HEALTHY, HealthState.WATCH, HealthState.DECAYING,
           HealthState.RETIRE_RECOMMENDED]


def _down_one(state: HealthState) -> HealthState:
    return _LADDER[max(0, _LADDER.index(state) - 1)]


def advance(state: HealthState, decline_streak: int, recover_streak: int,
            signal: Signal, cfg: StrategyHealthConfig) -> tuple[HealthState, int, int]:
    """Hysteresis transition. Only DYING escalates toward retirement."""
    if signal is Signal.DYING:
        d = decline_streak + 1
        if state in (HealthState.HEALTHY, HealthState.WATCH):
            state = HealthState.DECAYING if d >= cfg.decay_streak else HealthState.WATCH
        elif state is HealthState.DECAYING and d >= cfg.retire_streak:
            state = HealthState.RETIRE_RECOMMENDED
        return state, d, 0
    if signal is Signal.SOFTENING:
        if state is HealthState.HEALTHY:
            state = HealthState.WATCH
        return state, 0, 0                    # not dying: reset decline, no recovery
    if signal is Signal.OK:
        r = recover_streak + 1
        if state is not HealthState.HEALTHY and r >= cfg.recover_streak:
            return _down_one(state), 0, 0
        return state, 0, r
    return state, decline_streak, recover_streak     # INSUFFICIENT holds everything
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_decay_advance.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/analytics/decay.py tests/unit/test_decay_advance.py
git commit -m "feat(analytics): strategy-health hysteresis state machine"
```

---

### Task 4: `StrategyHealth` model + storage

**Files:**
- Modify: `src/poseidon/core/models.py` (add `StrategyHealth`)
- Modify: `src/poseidon/storage/db.py` (DDL + `upsert_strategy_health`, `get_strategy_health`, `list_strategy_health`)
- Test: `tests/unit/test_strategy_health_storage.py`

**Interfaces:**
- Produces: `StrategyHealth(strategy, state, decline_streak, recover_streak, window_return, baseline_return, t_stat, trades, updated_at)`; `Database.upsert_strategy_health(h)`, `.get_strategy_health(strategy) -> StrategyHealth | None`, `.list_strategy_health() -> list[StrategyHealth]`.

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_strategy_health_storage.py
from __future__ import annotations

from datetime import UTC, datetime

from poseidon.core.models import StrategyHealth
from poseidon.storage.db import Database


def _h(strategy: str, state: str) -> StrategyHealth:
    return StrategyHealth(strategy=strategy, state=state, decline_streak=1, recover_streak=0,
                          window_return=-0.01, baseline_return=0.02, t_stat=-3.1, trades=10,
                          updated_at=datetime.now(UTC))


async def test_upsert_get_list(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    await db.upsert_strategy_health(_h("alpha", "decaying"))
    await db.upsert_strategy_health(_h("alpha", "retire_recommended"))   # upsert same PK
    got = await db.get_strategy_health("alpha")
    assert got is not None and got.state == "retire_recommended"          # latest wins
    assert len(await db.list_strategy_health()) == 1
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_strategy_health_storage.py -q`
Expected: FAIL — `ImportError` on `StrategyHealth`.

- [ ] **Step 3: Write minimal implementation**

In `core/models.py`, add (a `PoseidonModel`):
```python
class StrategyHealth(PoseidonModel):
    """Advisory decay state for one strategy. Derived (not an audit fact); its own table."""

    strategy: str
    state: str
    decline_streak: int = 0
    recover_streak: int = 0
    window_return: float = 0.0
    baseline_return: float = 0.0
    t_stat: float = 0.0
    trades: int = 0
    updated_at: datetime
```
Append DDL to the `_SCHEMA` string in `db.py`:
```sql
CREATE TABLE IF NOT EXISTS strategy_health (
    strategy TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```
Add methods on `Database` (importing `StrategyHealth`), `_row_to_health` decoding `payload`:
```python
    async def upsert_strategy_health(self, h: StrategyHealth) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO strategy_health (strategy, state, payload, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (h.strategy, h.state, h.model_dump_json(), h.updated_at.isoformat()))

    async def get_strategy_health(self, strategy: str) -> StrategyHealth | None:
        row = await self.fetch_one(
            "SELECT payload FROM strategy_health WHERE strategy = ?", (strategy,))
        return StrategyHealth.model_validate_json(row[0]) if row else None

    async def list_strategy_health(self) -> list[StrategyHealth]:
        rows = await self.fetch_all("SELECT payload FROM strategy_health ORDER BY strategy")
        return [StrategyHealth.model_validate_json(r[0]) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_strategy_health_storage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/core/models.py src/poseidon/storage/db.py tests/unit/test_strategy_health_storage.py
git commit -m "feat(storage): strategy_health model + table"
```

---

### Task 5: `StrategyHealthService` — sweep, audit, notify, retire

**Files:**
- Create: `src/poseidon/analytics/decay_service.py`
- Test: `tests/unit/test_strategy_health_service.py`

**Interfaces:**
- Consumes: `Database` (Task 4), `StrategyHealthConfig`, `assess`/`advance`/`HealthState` (Tasks 2-3), `RoundTrip`.
- Produces: `StrategyHealthService(db, config, load_trips, audit_append, notify, retire)` with `sweep()` and `report()`.

- [ ] **Step 1: Write the failing test** (the safety invariant — retire never fires when auto_retire=False)
```python
# tests/unit/test_strategy_health_service.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.analytics.decay_service import StrategyHealthService
from poseidon.analytics.performance import RoundTrip
from poseidon.core.config import StrategyHealthConfig
from poseidon.storage.db import Database


def _trip(r: float, day: int) -> RoundTrip:
    e = Decimal("100")
    return RoundTrip(symbol="X", strategy="s", quantity=Decimal("1"), entry_price=e,
                     exit_price=e * Decimal(str(1 + r)), entered_at=datetime(2024, 1, 1, tzinfo=UTC),
                     exited_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day))


async def _svc(db, cfg, retired, audits, notes):
    async def _audit(actor, action, data): audits.append((actor, action))
    async def _notify(level, data): notes.append(level)
    async def _retire(name): retired.append(name); return True
    # 25 profitable then 25 losing trades: with the default window_trades=20 the window
    # is cleanly negative (baseline >= 20) -> a clean DYING assessment.
    dying = [_trip(0.02, d) for d in range(25)] + [_trip(-0.015, 25 + d) for d in range(25)]
    async def _load(): return dying
    return StrategyHealthService(db=db, config=cfg, load_trips=_load, audit_append=_audit,
                                 notify=_notify, retire=_retire)


async def test_retire_never_called_when_auto_retire_off(tmp_path) -> None:
    db = Database(tmp_path / "t.db"); await db.open()
    retired: list = []; audits: list = []; notes: list = []
    cfg = StrategyHealthConfig(auto_retire=False, decay_streak=1, retire_streak=1)
    svc = await _svc(db, cfg, retired, audits, notes)
    for _ in range(5):
        await svc.sweep()
    assert retired == []                              # advisory only — never retires
    h = await db.get_strategy_health("s")
    assert h is not None and h.state in {"decaying", "retire_recommended"}   # but it IS flagged
    await db.close()


async def test_retire_fires_only_on_recommendation_when_enabled(tmp_path) -> None:
    db = Database(tmp_path / "t.db"); await db.open()
    retired: list = []; audits: list = []; notes: list = []
    cfg = StrategyHealthConfig(auto_retire=True, decay_streak=1, retire_streak=2)
    svc = await _svc(db, cfg, retired, audits, notes)
    for _ in range(4):
        await svc.sweep()
    assert retired == ["s"]                            # exactly once, at retire_recommended
    await db.close()


def test_service_holds_no_execution_refs() -> None:
    import inspect
    import poseidon.analytics.decay_service as m
    src = inspect.getsource(m)
    for banned in ("RiskEngine", "OrderManager", "submit_decision", "Broker(", "broker"):
        assert banned not in src                      # constructive isolation from the order path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_strategy_health_service.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`src/poseidon/analytics/decay_service.py`:
```python
"""Strategy-decay watchdog service. Best-effort, scheduled; reduce-only (its only
mutation is deactivating a decayed custom strategy). Never imports the risk engine,
the order manager, or a broker."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from ..core.config import StrategyHealthConfig
from ..core.models import StrategyHealth
from ..storage.db import Database
from .decay import HealthState, Signal, advance, assess
from .performance import RoundTrip

log = structlog.get_logger(__name__)

_DOWNGRADES = {HealthState.DECAYING, HealthState.RETIRE_RECOMMENDED}


class StrategyHealthService:
    def __init__(self, *, db: Database, config: StrategyHealthConfig,
                 load_trips: Callable[[], Awaitable[list[RoundTrip]]],
                 audit_append: Callable[[str, str, dict[str, Any]], Awaitable[Any]],
                 notify: Callable[[str, dict[str, Any]], Awaitable[Any]],
                 retire: Callable[[str], Awaitable[bool]]) -> None:
        self._db = db
        self._config = config
        self._load_trips = load_trips
        self._audit = audit_append
        self._notify = notify
        self._retire = retire

    async def sweep(self, _topic: str | None = None, _payload: object = None) -> None:
        if not self._config.enabled:
            return
        try:
            trips = await self._load_trips()
        except Exception as exc:
            log.warning("strategy-health load failed", error=str(exc))
            return
        by_strategy: dict[str, list[RoundTrip]] = {}
        for t in trips:
            by_strategy.setdefault(t.strategy or "unattributed", []).append(t)
        for strategy, strat_trips in by_strategy.items():
            try:
                await self._evaluate(strategy, strat_trips)
            except Exception as exc:            # one bad strategy can't break the sweep
                log.warning("strategy-health eval failed", strategy=strategy, error=str(exc))

    async def _evaluate(self, strategy: str, trips: list[RoundTrip]) -> None:
        prior = await self._db.get_strategy_health(strategy)
        state = HealthState(prior.state) if prior else HealthState.HEALTHY
        decline = prior.decline_streak if prior else 0
        recover = prior.recover_streak if prior else 0
        a = assess(trips, self._config)
        new_state, decline, recover = advance(state, decline, recover, a.signal, self._config)
        await self._db.upsert_strategy_health(StrategyHealth(
            strategy=strategy, state=new_state.value, decline_streak=decline,
            recover_streak=recover, window_return=a.window_return,
            baseline_return=a.baseline_return, t_stat=a.t0, trades=a.trades,
            updated_at=datetime.now(UTC)))
        if new_state is state:
            return
        await self._audit("system", "strategy.health_changed",
                          {"strategy": strategy, "from": state.value, "to": new_state.value})
        if new_state in _DOWNGRADES:
            await self._notify("warning", {"strategy": strategy, "state": new_state.value,
                                           "window_return": round(a.window_return, 4)})
        if (self._config.auto_retire and new_state is HealthState.RETIRE_RECOMMENDED):
            did = await self._retire(strategy)          # only deactivates a custom strategy
            if did:
                await self._audit("system", "strategy.auto_retired", {"strategy": strategy})

    async def report(self) -> list[StrategyHealth]:
        try:
            return await self._db.list_strategy_health()
        except Exception as exc:
            log.warning("strategy-health report failed", error=str(exc))
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_strategy_health_service.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/analytics/decay_service.py tests/unit/test_strategy_health_service.py
git commit -m "feat(analytics): StrategyHealthService — sweep, audit, notify, reduce-only retire"
```

---

### Task 6: Wire into the kernel + schedule

**Files:**
- Modify: `src/poseidon/app.py` (construct the service in `start()`; a `_load_strategy_trips` loader; a `_retire_strategy` adapter; register `strategy_health_sweep`; default schedule in `_effective_schedules`)
- Test: `tests/unit/test_strategy_health_wiring.py`

**Interfaces:**
- Consumes: `StrategyHealthService`, the existing fill→round-trip loader, `AlgorithmWorkshop.list_all`/`deactivate`, `NotificationService`, `Scheduler`.
- Produces: `kernel.strategy_health: StrategyHealthService | None`; a `strategy_health_sweep` job; a default daily schedule when `enabled`.

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_strategy_health_wiring.py
from __future__ import annotations

from types import SimpleNamespace

from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig, ScheduleConfig
from poseidon.security.vault import Vault


def test_default_schedule_only_when_enabled(tmp_path) -> None:
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    scheds = kernel._effective_schedules()
    assert any(s.job == "strategy_health_sweep" for s in scheds)          # default present
    off = ApplicationKernel(
        AppConfig(strategy_health={"enabled": False}), Vault(tmp_path / "v2.bin"))
    assert not any(s.job == "strategy_health_sweep" for s in off._effective_schedules())


def test_retire_adapter_is_reduce_only_for_unknown(tmp_path) -> None:
    # a strategy with no active custom algorithm -> adapter returns False (flag-only), never raises
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    kernel.workshop = SimpleNamespace(list_all=_none, deactivate=_boom)   # deactivate must not run
    import asyncio
    assert asyncio.run(kernel._retire_strategy("nonexistent")) is False


async def _none() -> list:
    return []


async def _boom(*a, **k):
    raise AssertionError("deactivate must not be called for an unknown/builtin strategy")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_strategy_health_wiring.py -q`
Expected: FAIL — no `strategy_health_sweep` schedule / no `_retire_strategy`.

- [ ] **Step 3: Wire it**

In `app.py` `start()` (after `self.workshop`, `self.notifier`, `self.sync` exist), construct:
```python
        self.strategy_health = StrategyHealthService(
            db=self.db, config=cfg.strategy_health,
            load_trips=self._load_strategy_trips,
            audit_append=self.audit.append,
            notify=lambda level, data: self.bus.publish(
                Topics.NOTIFY, {"level": level, "title": "strategy health", **data}),
            retire=self._retire_strategy)
```
Declare `self.strategy_health: StrategyHealthService | None = None` in `__init__`. Add helpers:
```python
    async def _load_strategy_trips(self) -> list[RoundTrip]:
        """All closed round-trips attributed per strategy (reuses the performance loader)."""
        fills = await self._load_all_fills()          # the existing per-order-attributed fills
        return build_round_trips(fills)

    async def _retire_strategy(self, strategy: str) -> bool:
        """Reduce-only: deactivate the ACTIVE CUSTOM strategy of this name, else no-op.
        Returns True iff it deactivated one. Never activates or orders."""
        try:
            for algo in await self.workshop.list_all():
                if algo.get("name") == strategy and algo.get("status") == "active":
                    await self.workshop.deactivate(algo["id"], archive=False)
                    return True
        except Exception as exc:
            log.warning("auto-retire failed", strategy=strategy, error=str(exc))
        return False
```
(Reuse or add `_load_all_fills` mirroring the existing performance fill-loader in `app.py`; import `RoundTrip`, `build_round_trips`, `StrategyHealthService`.) In `_register_jobs`: `self.scheduler.register_job("strategy_health_sweep", self.strategy_health.sweep)`. In `_effective_schedules`, add (mirroring the analysis-sweep default):
```python
        if self.config.strategy_health.enabled and not any(
            s.job == "strategy_health_sweep" and s.enabled for s in schedules):
            schedules.append(ScheduleConfig(name="default-strategy-health", job="strategy_health_sweep",
                                            cron="0 6 * * *"))   # daily pre-market
```

- [ ] **Step 4: Run the gate**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest -q`
Expected: ruff clean, mypy `Success`, all pass.

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/app.py tests/unit/test_strategy_health_wiring.py
git commit -m "feat(app): wire the strategy-decay watchdog + daily schedule"
```

---

### Task 7: Docs + example config

**Files:**
- Modify: `config/poseidon.example.yaml` (a `strategy_health:` block)
- Modify: `docs/api-configuration.md` (a "Strategy-decay watchdog" section)
- Test: none — run the full gate.

- [ ] **Step 1: Add the commented example** under a top-level `strategy_health:` in `config/poseidon.example.yaml`:
```yaml
# Strategy-decay watchdog. Watches each strategy's rolling realized edge and flags
# decay (healthy -> watch -> decaying -> retire_recommended). ADVISORY by default —
# it only flags/audits/notifies. It can only ever REDUCE trading, never place an
# order. auto_retire (opt-in) deactivates a decayed CUSTOM strategy (builtin = flag-only).
strategy_health:
  enabled: true
  auto_retire: false      # opt-in: auto-deactivate a decayed custom strategy
  window_trades: 20
  min_trades: 8           # below this the window is "insufficient" (never flagged decaying)
  baseline_min_trades: 20
  decay_t: 2.0            # t-stat threshold for a significantly-<=0 recent edge
  decay_streak: 2         # consecutive dying sweeps -> decaying
  retire_streak: 4        # consecutive dying sweeps -> retire_recommended
  recover_streak: 2       # consecutive ok sweeps to step back toward healthy
```

- [ ] **Step 2: Add a docs section** ("Strategy-decay watchdog") covering: what it monitors (rolling realized edge), the states + hysteresis, that only a genuinely-unprofitable edge escalates (a lower-but-positive edge is just "softening"), the conservative-on-few-trades caveat, the reduce-only/advisory-default safety posture, and `auto_retire`'s custom-only scope.

- [ ] **Step 3: Run the full gate**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**
```bash
git add config/poseidon.example.yaml docs/api-configuration.md
git commit -m "docs(analytics): document the strategy-decay watchdog + example config"
```

---

## After the plan

Final whole-branch review (most-capable model) on the **reduce-only safety invariant** (never the risk/order path; auto_retire scoped + audited; exits not orphaned — already verified) and the **decay ≠ normalization** logic (only a genuinely-negative edge escalates). Then release **v2.12.0** — the final release of the four-part cross-pollination program. Bump the three version files; PR `feat/strategy-decay-tracking` → main; **explicit merge sign-off required** (per the classifier gotcha — ask up front); tag + GitHub release; remind to revoke the token.
