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
    async def _retire(name):
        retired.append(name)
        return True
    # 25 profitable then 25 losing trades: with the default window_trades=20 the window
    # is cleanly negative (baseline >= 20) -> a clean DYING assessment.
    dying = [_trip(0.02, d) for d in range(25)] + [_trip(-0.015, 25 + d) for d in range(25)]
    async def _load(): return dying
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


def test_service_holds_no_execution_refs() -> None:
    import inspect

    import poseidon.analytics.decay_service as m
    src = inspect.getsource(m)
    for banned in ("RiskEngine", "OrderManager", "submit_decision", "Broker(", "broker"):
        assert banned not in src                      # constructive isolation from the order path
