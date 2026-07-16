# tests/unit/test_strategy_health_service.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.analytics.decay import HealthState
from poseidon.analytics.decay_service import StrategyHealthService
from poseidon.analytics.performance import RoundTrip
from poseidon.core.config import StrategyHealthConfig
from poseidon.core.models import StrategyHealth
from poseidon.storage.db import Database


def _trip(r: float, day: int) -> RoundTrip:
    e = Decimal("100")
    return RoundTrip(symbol="X", strategy="s", quantity=Decimal("1"), entry_price=e,
                     exit_price=e * Decimal(str(1 + r)), entered_at=datetime(2024, 1, 1, tzinfo=UTC),
                     exited_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day))


async def _svc(db, cfg, retired, audits, notes):
    async def _audit(actor, action, data): audits.append((actor, action))
    async def _notify(level, data): notes.append(level)
    async def _retire(name):
        # Mirrors the real retire contract: idempotent, True only on the call
        # that actually deactivates.
        if name in retired:
            return False
        retired.append(name)
        return True
    # 25 profitable then a growing tail of losing trades: with the default
    # window_trades=20 the window is cleanly negative (baseline >= 20) -> a
    # clean DYING assessment, and each sweep sees one NEW losing trade (a
    # frozen window deliberately never advances the streak).
    trips = [_trip(0.02, d) for d in range(25)] + [_trip(-0.015, 25 + d) for d in range(25)]
    async def _load():
        trips.append(_trip(-0.015, 25 + len(trips)))
        return list(trips)
    return StrategyHealthService(db=db, config=cfg, load_trips=_load, audit_append=_audit,
                                 notify=_notify, retire=_retire)


async def test_retire_never_called_when_auto_retire_off(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    retired: list = []
    audits: list = []
    notes: list = []
    cfg = StrategyHealthConfig(auto_retire=False, decay_streak=1, retire_streak=1)
    svc = await _svc(db, cfg, retired, audits, notes)
    for _ in range(5):
        await svc.sweep()
    assert retired == []                              # advisory only — never retires
    h = await db.get_strategy_health("s")
    assert h is not None and h.state in {"decaying", "retire_recommended"}   # but it IS flagged
    await db.close()


async def test_retire_fires_only_on_recommendation_when_enabled(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    retired: list = []
    audits: list = []
    notes: list = []
    cfg = StrategyHealthConfig(auto_retire=True, decay_streak=1, retire_streak=2)
    svc = await _svc(db, cfg, retired, audits, notes)
    for _ in range(4):
        await svc.sweep()
    assert retired == ["s"]                            # exactly once, at retire_recommended
    await db.close()


async def _svc_with_trips(db, cfg, trips, notes):
    async def _audit(actor, action, data): pass
    async def _notify(level, data): notes.append(level)
    async def _retire(name): return True
    async def _load(): return trips
    return StrategyHealthService(db=db, config=cfg, load_trips=_load, audit_append=_audit,
                                 notify=_notify, retire=_retire)


async def test_recovery_downgrade_state_does_not_notify(tmp_path) -> None:
    # A rung-by-rung RECOVERY (retire_recommended -> decaying) lands on a state
    # that is a member of _DOWNGRADES, but it is an IMPROVEMENT, not a decline
    # -- it must never fire the "warning" notification.
    db = Database(tmp_path / "t.db")
    await db.open()
    notes: list = []
    cfg = StrategyHealthConfig(recover_streak=3)
    await db.upsert_strategy_health(StrategyHealth(
        strategy="s", state=HealthState.RETIRE_RECOMMENDED.value,
        recover_streak=cfg.recover_streak - 1, updated_at=datetime.now(UTC)))
    # 45 uniformly profitable trips -> a clean (degenerate-variance) Signal.OK.
    ok_trips = [_trip(0.02, d) for d in range(45)]
    svc = await _svc_with_trips(db, cfg, ok_trips, notes)
    await svc.sweep()
    h = await db.get_strategy_health("s")
    assert h is not None and h.state == "decaying"            # recovered exactly one rung
    assert notes == []                                        # a recovery must never warn
    await db.close()


async def test_genuine_downgrade_does_notify(tmp_path) -> None:
    # Contrast case: a real healthy -> decaying decline DOES warn.
    db = Database(tmp_path / "t.db")
    await db.open()
    notes: list = []
    cfg = StrategyHealthConfig(decay_streak=1)
    # 45 uniformly losing trades -> a clean (degenerate-variance) Signal.DYING.
    dying_trips = [_trip(-0.02, d) for d in range(45)]
    svc = await _svc_with_trips(db, cfg, dying_trips, notes)
    await svc.sweep()
    h = await db.get_strategy_health("s")
    assert h is not None and h.state == "decaying"
    assert notes == ["warning"]                               # a genuine downgrade DOES warn
    await db.close()


def test_service_holds_no_execution_refs() -> None:
    import inspect

    import poseidon.analytics.decay_service as m
    src = inspect.getsource(m)
    for banned in ("RiskEngine", "OrderManager", "submit_decision", "Broker(", "broker"):
        assert banned not in src                      # constructive isolation from the order path
