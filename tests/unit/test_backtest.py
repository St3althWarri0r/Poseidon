"""Backtest engine, Monte Carlo, walk-forward, stress tests."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.backtest.analysis import monte_carlo, stress_test, walk_forward
from poseidon.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from poseidon.core.models import Bar
from poseidon.strategy.builtin.trend import MomentumStrategy


def synthetic_history(symbol: str = "TEST", days: int = 250, drift: float = 0.001,
                      wobble: float = 0.01) -> dict[str, list[Bar]]:
    bars: list[Bar] = []
    price = 100.0
    start = datetime(2025, 1, 2, tzinfo=UTC)
    for i in range(days):
        day = start + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        price *= 1 + drift + wobble * math.sin(i / 9)
        bars.append(
            Bar(symbol=symbol, open=Decimal(str(round(price * 0.999, 4))),
                high=Decimal(str(round(price * 1.005, 4))),
                low=Decimal(str(round(price * 0.995, 4))),
                close=Decimal(str(round(price, 4))),
                volume=1_000_000, start=day, end=day, source="synthetic")
        )
    return {symbol: bars}


async def test_backtest_runs_and_reports() -> None:
    history = synthetic_history()
    engine = BacktestEngine(BacktestConfig(starting_cash=100_000))
    result = await engine.run(MomentumStrategy(symbols=["TEST"]), history)
    assert len(result.equity_curve) > 100
    summary = result.summary()
    assert set(summary) == {"total_return", "max_drawdown", "sharpe", "trades",
                            "win_rate", "final_equity"}
    # Uptrending synthetic series + momentum should trade and end positive.
    assert summary["trades"] >= 1
    assert summary["final_equity"] > 0


async def test_no_lookahead_first_days_have_no_trades() -> None:
    history = synthetic_history(days=250)
    engine = BacktestEngine()
    result = await engine.run(MomentumStrategy(symbols=["TEST"]), history)
    # Momentum needs ~50 bars of history; nothing can trade in the first 30 days.
    early_cutoff = result.equity_curve[30][0]
    assert all(t.entry_date > early_cutoff for t in result.trades)


async def test_monte_carlo_summary() -> None:
    history = synthetic_history()
    result = await BacktestEngine().run(MomentumStrategy(symbols=["TEST"]), history)
    mc = monte_carlo(result, runs=200, seed=42)
    assert mc.runs == 200
    assert mc.p05_return <= mc.median_return <= mc.p95_return
    assert 0 <= mc.prob_loss <= 1
    assert mc.p95_max_drawdown >= mc.median_max_drawdown


def test_monte_carlo_needs_history() -> None:
    empty = BacktestResult()
    with pytest.raises(ValueError):
        monte_carlo(empty)


async def test_walk_forward_folds() -> None:
    history = synthetic_history(days=400)
    reports = await walk_forward(lambda: MomentumStrategy(symbols=["TEST"]), history, folds=3)
    assert len(reports) == 3
    assert all("total_return" in r for r in reports)
    # Folds are sequential in time.
    assert reports[0]["end"] < reports[1]["start"] or reports[0]["end"] <= reports[1]["start"]


async def test_stress_scenarios() -> None:
    history = synthetic_history()
    result = await BacktestEngine().run(MomentumStrategy(symbols=["TEST"]), history)
    reports = stress_test(result)
    assert {r["scenario"] for r in reports} >= {"black_monday_1987", "covid_mar_2020"}
    for report in reports:
        assert 0 < report["total_drawdown"] < 1
