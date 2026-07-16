# tests/unit/test_audit_decay.py
"""Decay-watchdog audit regressions: a frozen trade window must not advance
hysteresis streaks (17), transition side effects must survive transient
failures without being lost or duplicated (18), and strategy_health rows are
account-scoped like the fills that feed them (19)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.analytics.decay_service import StrategyHealthService
from poseidon.analytics.performance import RoundTrip
from poseidon.core.config import StrategyHealthConfig
from poseidon.core.models import StrategyHealth
from poseidon.storage.db import Database


def _trip(r: float, day: int) -> RoundTrip:
    e = Decimal("100")
    return RoundTrip(symbol="X", strategy="s", quantity=Decimal("1"), entry_price=e,
                     exit_price=e * Decimal(str(1 + r)),
                     entered_at=datetime(2024, 1, 1, tzinfo=UTC),
                     exited_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day))


def _health(strategy: str, state: str) -> StrategyHealth:
    return StrategyHealth(strategy=strategy, state=state, decline_streak=1,
                          window_return=-0.01, t_stat=-3.0, trades=10,
                          updated_at=datetime.now(UTC))


# -- finding 17: streaks must count new evidence, not calendar sweeps ----------


async def test_frozen_window_never_escalates_past_first_assessment(tmp_path) -> None:
    # 4 sweeps over the IDENTICAL closed-trade history (weekend/holiday sweeps):
    # the first assessment's verdict must hold — zero new trades means zero new
    # confirmations, so retire_streak hysteresis cannot be satisfied by time alone.
    db = Database(tmp_path / "t.db")
    await db.open()
    retired: list = []
    dying = [_trip(-0.02, d) for d in range(45)]

    async def _load() -> list[RoundTrip]:
        return dying
    async def _audit(actor, action, data) -> None: pass
    async def _notify(level, data) -> None: pass
    async def _retire(name) -> bool:
        retired.append(name)
        return True
    cfg = StrategyHealthConfig(auto_retire=True, decay_streak=1, retire_streak=2)
    svc = StrategyHealthService(db=db, config=cfg, load_trips=_load, audit_append=_audit,
                                notify=_notify, retire=_retire)
    for _ in range(4):
        await svc.sweep()
    h = await db.get_strategy_health("s")
    assert h is not None and h.state == "decaying"     # first assessment, held
    assert h.decline_streak == 1                       # never advanced without evidence
    assert retired == []                               # and never auto-retired
    await db.close()


async def test_new_closed_trade_advances_the_streak(tmp_path) -> None:
    # Counterpart guard: a genuinely NEW losing trade between sweeps is new
    # evidence and must keep escalating toward retirement.
    db = Database(tmp_path / "t.db")
    await db.open()
    trips = [_trip(-0.02, d) for d in range(45)]

    async def _load() -> list[RoundTrip]:
        return list(trips)
    async def _audit(actor, action, data) -> None: pass
    async def _notify(level, data) -> None: pass
    async def _retire(name) -> bool: return False
    cfg = StrategyHealthConfig(decay_streak=1, retire_streak=2)
    svc = StrategyHealthService(db=db, config=cfg, load_trips=_load, audit_append=_audit,
                                notify=_notify, retire=_retire)
    await svc.sweep()
    trips.append(_trip(-0.02, 45))                     # one fresh confirmation
    await svc.sweep()
    h = await db.get_strategy_health("s")
    assert h is not None and h.state == "retire_recommended" and h.decline_streak == 2
    await db.close()


# -- finding 18: transition side effects must be retryable, not one-shot -------


async def test_failed_notify_leaves_state_unconsumed_then_retries_exactly_once(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    notes: list = []
    calls = {"n": 0}

    async def _notify(level, data) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("notification channel down")
        notes.append(level)
    async def _load() -> list[RoundTrip]:
        return [_trip(-0.02, d) for d in range(45)]
    async def _audit(actor, action, data) -> None: pass
    async def _retire(name) -> bool: return False
    cfg = StrategyHealthConfig(decay_streak=1)
    svc = StrategyHealthService(db=db, config=cfg, load_trips=_load, audit_append=_audit,
                                notify=_notify, retire=_retire)
    await svc.sweep()                                  # notify fails mid-transition
    assert await db.get_strategy_health("s") is None   # state NOT consumed
    await svc.sweep()                                  # channel healed: retried
    h = await db.get_strategy_health("s")
    assert h is not None and h.state == "decaying"
    await svc.sweep()                                  # committed: never notifies again
    assert notes == ["warning"]                        # exactly one successful warning
    await db.close()


async def test_transient_retire_failure_is_reattempted_and_audited_once(tmp_path) -> None:
    # _retire_strategy swallows transient errors and returns False; the sweep
    # must keep re-attempting an unconsummated retirement (idempotently) instead
    # of dropping it forever after the one-shot transition.
    db = Database(tmp_path / "t.db")
    await db.open()
    audits: list = []
    notes: list = []
    attempts: list = []
    outcomes = [False, True]                           # transient failure, then success

    async def _retire(name) -> bool:
        attempts.append(name)
        return outcomes.pop(0) if outcomes else False  # False once retired: idempotent
    async def _audit(actor, action, data) -> None:
        audits.append(action)
    async def _notify(level, data) -> None:
        notes.append(level)
    trips = [_trip(-0.02, d) for d in range(45)]

    async def _load() -> list[RoundTrip]:
        return list(trips)
    cfg = StrategyHealthConfig(auto_retire=True, decay_streak=1, retire_streak=2)
    svc = StrategyHealthService(db=db, config=cfg, load_trips=_load, audit_append=_audit,
                                notify=_notify, retire=_retire)
    await svc.sweep()                                  # healthy -> decaying
    trips.append(_trip(-0.02, 45))
    await svc.sweep()                                  # -> retire_recommended; retire fails
    h = await db.get_strategy_health("s")
    assert h is not None and h.state == "retire_recommended"
    assert audits.count("strategy.auto_retired") == 0
    await svc.sweep()                                  # frozen evidence: retry succeeds
    await svc.sweep()                                  # already retired: no duplicate audit
    assert len(attempts) == 3
    assert audits.count("strategy.auto_retired") == 1
    assert notes == ["warning", "warning"]             # one per genuine downgrade, no dupes
    await db.close()


# -- finding 19: health rows are account-scoped like their input fills ---------


async def test_health_rows_are_account_scoped(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    await db.upsert_strategy_health(_health("algo:X", "retire_recommended"),
                                    account_scope="alpaca:paper")
    assert await db.get_strategy_health("algo:X", account_scope="alpaca:live") is None
    await db.upsert_strategy_health(_health("algo:X", "healthy"), account_scope="alpaca:live")
    paper = await db.get_strategy_health("algo:X", account_scope="alpaca:paper")
    assert paper is not None and paper.state == "retire_recommended"   # scopes never clobber
    live_rows = await db.list_strategy_health(account_scope="alpaca:live")
    assert [h.state for h in live_rows] == ["healthy"]
    await db.close()


async def test_account_switch_starts_fresh_health_state(tmp_path) -> None:
    # A paper-era retire_recommended verdict must neither appear in the live
    # report nor seed the live account's hysteresis streaks.
    db = Database(tmp_path / "t.db")
    await db.open()
    async def _audit(actor, action, data) -> None: pass
    async def _notify(level, data) -> None: pass
    async def _retire(name) -> bool: return False
    cfg = StrategyHealthConfig(decay_streak=1, retire_streak=2)
    paper_trips = [_trip(-0.02, d) for d in range(45)]

    async def _load_paper() -> list[RoundTrip]:
        return list(paper_trips)
    paper = StrategyHealthService(db=db, config=cfg, load_trips=_load_paper,
                                  audit_append=_audit, notify=_notify, retire=_retire,
                                  account_scope="alpaca:paper")
    await paper.sweep()
    paper_trips.append(_trip(-0.02, 45))
    await paper.sweep()
    ph = await db.get_strategy_health("s", account_scope="alpaca:paper")
    assert ph is not None and ph.state == "retire_recommended"

    live_trips: list[RoundTrip] = []

    async def _load_live() -> list[RoundTrip]:
        return list(live_trips)
    live = StrategyHealthService(db=db, config=cfg, load_trips=_load_live,
                                 audit_append=_audit, notify=_notify, retire=_retire,
                                 account_scope="alpaca:live")
    await live.sweep()
    assert await live.report() == []                   # paper verdict not resurrected
    live_trips.extend(_trip(-0.02, d) for d in range(45))
    await live.sweep()
    lh = await db.get_strategy_health("s", account_scope="alpaca:live")
    assert lh is not None and lh.state == "decaying" and lh.decline_streak == 1
    ph = await db.get_strategy_health("s", account_scope="alpaca:paper")
    assert ph is not None and ph.state == "retire_recommended"   # paper row untouched
    await db.close()


async def test_legacy_strategy_health_table_migrates_to_scoped_rows(tmp_path) -> None:
    # Pre-migration rows keep scope='' and drop out of scoped reads, matching
    # the orders/equity_marks convention.
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE strategy_health (strategy TEXT PRIMARY KEY, state TEXT NOT NULL, "
        "payload TEXT NOT NULL, updated_at TEXT NOT NULL);")
    h = _health("algo:Old", "retire_recommended")
    conn.execute("INSERT INTO strategy_health VALUES (?, ?, ?, ?)",
                 ("algo:Old", h.state, h.model_dump_json(), h.updated_at.isoformat()))
    conn.commit()
    conn.close()
    db = Database(path)
    await db.open()
    assert await db.get_strategy_health("algo:Old", account_scope="alpaca:live") is None
    legacy = await db.get_strategy_health("algo:Old")            # scope='' keeps history
    assert legacy is not None and legacy.state == "retire_recommended"
    await db.close()
