from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.analytics.decay import Signal, assess
from poseidon.analytics.performance import RoundTrip
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
