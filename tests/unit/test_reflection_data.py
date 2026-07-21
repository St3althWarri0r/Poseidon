from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.analytics.performance import FillRecord
from poseidon.analytics.reflection_data import (
    benchmark_return,
    forward_return,
    latest_closed_episode,
)
from poseidon.core.enums import OrderSide
from poseidon.core.models import Bar


def _fill(side, qty, price, day, *, did="d1", strat="mom"):
    return FillRecord(symbol="SPY", side=side, quantity=Decimal(qty),
                      price=Decimal(price), at=datetime(2026, 6, day, tzinfo=UTC),
                      strategy=strat, decision_id=did)


def test_simple_round_trip_episode() -> None:
    fills = [_fill(OrderSide.BUY, "10", "100", 1), _fill(OrderSide.SELL, "10", "110", 4)]
    ep = latest_closed_episode(fills)
    assert ep is not None
    assert ep.symbol == "SPY" and ep.is_short is False and ep.quantity == Decimal("10")
    assert ep.entry_price == Decimal("100") and ep.exit_price == Decimal("110")
    assert abs(ep.realized_return - 0.10) < 1e-9
    assert ep.holding_days == 3.0 and ep.decision_id == "d1" and ep.strategy == "mom"


def test_scale_out_aggregates_to_one_episode() -> None:
    fills = [_fill(OrderSide.BUY, "10", "100", 1),
             _fill(OrderSide.SELL, "4", "110", 3),
             _fill(OrderSide.SELL, "6", "120", 5)]
    ep = latest_closed_episode(fills)
    assert ep is not None and ep.quantity == Decimal("10")
    # weighted exit = (4*110 + 6*120)/10 = 116; return = (116-100)/100
    assert ep.exit_price == Decimal("116")
    assert abs(ep.realized_return - 0.16) < 1e-9


def test_still_open_returns_none() -> None:
    fills = [_fill(OrderSide.BUY, "10", "100", 1), _fill(OrderSide.SELL, "4", "110", 3)]
    assert latest_closed_episode(fills) is None


def test_short_episode_return_sign() -> None:
    fills = [_fill(OrderSide.SELL_TO_OPEN, "10", "100", 1),
             _fill(OrderSide.BUY_TO_CLOSE, "10", "90", 3)]
    ep = latest_closed_episode(fills)
    assert ep is not None and ep.is_short is True
    assert abs(ep.realized_return - 0.10) < 1e-9  # shorted at 100, covered at 90 = +10%


def _bar(day, close):
    t = datetime(2026, 6, day, tzinfo=UTC)
    return Bar(symbol="SPY", open=Decimal(close), high=Decimal(close), low=Decimal(close),
               close=Decimal(close), volume=1, start=t, end=t, source="fake")


def test_benchmark_return_over_window() -> None:
    bars = [_bar(1, "400"), _bar(4, "412")]
    r = benchmark_return(bars, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 4, tzinfo=UTC))
    assert r is not None and abs(r - 0.03) < 1e-9


def test_benchmark_none_when_subday_or_gap() -> None:
    bars = [_bar(1, "400")]
    assert benchmark_return(bars, datetime(2026, 6, 1, tzinfo=UTC),
                            datetime(2026, 6, 1, 15, tzinfo=UTC)) is None
    assert benchmark_return([], datetime(2026, 6, 1, tzinfo=UTC),
                            datetime(2026, 6, 4, tzinfo=UTC)) is None


# ---- forward_return (outcome-resolution grading) ----------------------------


def test_forward_return_exact_horizon_bar_arithmetic() -> None:
    bars = [_bar(1, "100"), _bar(2, "110"), _bar(3, "121"), _bar(4, "133")]
    # Decision mid-day June 1: entry bar = FIRST bar ending at/after it (June 1),
    # exit = 2 bars later (June 3): 121/100 - 1.
    r = forward_return(bars, datetime(2026, 6, 1, tzinfo=UTC), 2)
    assert r is not None and abs(r - 0.21) < 1e-9


def test_forward_return_never_credits_pre_decision_drift() -> None:
    # A decision AFTER June 1's close must enter at June 2's close (the first
    # bar that ends at/after it) — the close_asof convention would grade from
    # June 1 and credit the counterfactual with same-day pre-decision drift,
    # inflating every "missed rally" lesson (the chasing bias itself).
    bars = [_bar(1, "100"), _bar(2, "110"), _bar(3, "121")]
    r = forward_return(bars, datetime(2026, 6, 1, 12, tzinfo=UTC), 1)
    assert r is not None and abs(r - (121 / 110 - 1)) < 1e-9


def test_forward_return_none_until_horizon_bars_exist() -> None:
    # Skip-and-retry contract: the series must extend `horizon` bars past the
    # entry bar before a grade exists.
    bars = [_bar(1, "100"), _bar(2, "110")]
    assert forward_return(bars, datetime(2026, 6, 1, tzinfo=UTC), 2) is None
    assert forward_return(bars, datetime(2026, 6, 1, tzinfo=UTC), 1) is not None


def test_forward_return_none_when_entry_bar_missing() -> None:
    bars = [_bar(1, "100"), _bar(2, "110")]
    assert forward_return(bars, datetime(2026, 6, 10, tzinfo=UTC), 1) is None
    assert forward_return([], datetime(2026, 6, 1, tzinfo=UTC), 1) is None


def test_forward_return_none_on_nonpositive_entry_close() -> None:
    bars = [_bar(1, "0"), _bar(2, "110"), _bar(3, "121")]
    assert forward_return(bars, datetime(2026, 6, 1, tzinfo=UTC), 1) is None


def test_forward_return_tolerates_unsorted_input() -> None:
    bars = [_bar(3, "121"), _bar(1, "100"), _bar(2, "110")]
    r = forward_return(bars, datetime(2026, 6, 1, tzinfo=UTC), 2)
    assert r is not None and abs(r - 0.21) < 1e-9
