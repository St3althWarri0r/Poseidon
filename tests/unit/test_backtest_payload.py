"""E2E payload, run-card, audit, and invariant tests for backtest evaluation depth."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.backtest.rebalance import rebalance_backtest
from poseidon.core.models import Bar
from poseidon.strategy.base import Signal, Strategy

# -- fixtures -----------------------------------------------------------------


def _bars(symbol: str, prices: list[float],
          start: datetime | None = None) -> list[Bar]:
    base = start or datetime(2023, 1, 2, tzinfo=UTC)
    bars: list[Bar] = []
    for i, p in enumerate(prices):
        day = base + timedelta(days=i)
        d = Decimal(str(round(p, 6)))
        bars.append(Bar(symbol=symbol, open=d, high=d * Decimal("1.01"),
                        low=d * Decimal("0.99"), close=d, volume=1_000_000,
                        start=day, end=day, source="synthetic"))
    return bars


class _HoldA(Strategy):
    name = "hold_a"

    async def scan(self, router, portfolio):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
        return [Signal(strategy=self.name, symbol="A", direction="long",
                       strength=1.0, evidence={"target_weight": 1.0})]


class _Rotate(Strategy):
    """Deterministically alternates 70/30 <-> 30/70 between A and B each day,
    forcing daily rebalance orders (cost + contribution accounting churn)."""

    name = "rotate"

    async def scan(self, router, portfolio):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
        bars = await router.bars("A", timeframe="1d", limit=500)
        wa = 0.7 if len(bars) % 2 == 0 else 0.3
        return [Signal(strategy=self.name, symbol="A", direction="long", strength=1.0,
                       evidence={"target_weight": wa}),
                Signal(strategy=self.name, symbol="B", direction="long", strength=1.0,
                       evidence={"target_weight": round(1.0 - wa, 4)})]


def _two_symbol_history(days: int = 320) -> dict[str, list[Bar]]:
    prices_a = [100.0 * 1.002 ** i for i in range(days)]
    prices_b = [100.0 * 0.999 ** i for i in range(days)]
    return {"A": _bars("A", prices_a), "B": _bars("B", prices_b)}


# -- config surface -----------------------------------------------------------


def test_backtest_eval_config_defaults_and_app_wiring() -> None:
    """ON by default under the SnapshotConfig precedent (deterministic, zero
    LLM cost, operator-facing only); every heavy knob individually
    disabled by 0; seed explicit and never wall-clock."""
    from poseidon.core.config import AppConfig, BacktestEvalConfig

    cfg = BacktestEvalConfig()
    assert (cfg.significance_runs, cfg.bootstrap_runs, cfg.monte_carlo_runs,
            cfg.walk_forward_folds, cfg.seed) == (1000, 1000, 1000, 3, 42)
    assert AppConfig().backtest == BacktestEvalConfig()
    off = BacktestEvalConfig(significance_runs=0, bootstrap_runs=0,
                             monte_carlo_runs=0, walk_forward_folds=0)
    assert off.significance_runs == 0 and off.walk_forward_folds == 0


# -- direct rebalance: conservation, wipeout, fallback, gating ----------------


async def test_rebalance_contribution_conservation() -> None:
    """sum(by_symbol contributions incl others) == final_equity - starting_cash
    (contributions are net of per-order costs). Pins the prev-mark /
    pre-rebalance-quantity accrual point."""
    report = await rebalance_backtest(_Rotate(symbols=["A", "B"]),
                                      _two_symbol_history(),
                                      commission_per_trade=0.5)
    attribution = report["attribution"]
    total = sum(v["contribution"] for s, v in attribution["by_symbol"].items()
                if s != "others")
    if "others" in attribution["by_symbol"]:
        total += attribution["by_symbol"]["others"]["contribution"]
    starting = report["starting_cash"]
    assert abs(total - (report["final_equity"] - starting)) < 1e-6 * starting
    assert attribution["trading_costs"] > 0
    assert report["turnover_gross"] > 0
    assert report["turnover_annual"] > report["turnover_gross"]  # 110-day window


async def test_rebalance_wipeout_reports_flat_minus_one_cagr() -> None:
    """A -99.9% collapapse must never be annualized: cagr == -1.0 flat plus an
    explicit warning, and the payload stays strict-JSON serializable."""
    prices = [100.0] * 210 + [100.0 * 0.94 ** i for i in range(1, 131)]
    report = await rebalance_backtest(_HoldA(symbols=["A"]),
                                      {"A": _bars("A", prices)})
    assert report["total_return"] < -0.99
    assert report["cagr"] == -1.0
    assert "wipeout_cagr_not_annualized" in report["warnings"]
    assert report["health"]["band"] == "at_risk"
    json.dumps(report, allow_nan=False)


async def test_rebalance_default_benchmark_is_equal_weight_fallback() -> None:
    """No benchmark supplied -> equal-weight universe, REPORTED via source and
    a warning — never a silent degrade."""
    report = await rebalance_backtest(_HoldA(symbols=["A"]), _two_symbol_history())
    bench = report["benchmark"]
    assert bench["source"] == "equal_weight_universe"
    assert bench["symbol"] == "EW(2)"
    assert "benchmark_fallback_equal_weight" in report["warnings"]
    assert bench["n_days"] > 60 and bench["beta"] is not None


async def test_rebalance_provider_benchmark_ols_and_annuals() -> None:
    days = 320
    prices_a = [100.0 * math.exp(0.0008 * i + 0.02 * math.sin(i / 7)) for i in range(days)]
    history = {"A": _bars("A", prices_a)}
    bench_closes = {b.start.date(): float(b.close) * 0.5 for b in history["A"]}
    report = await rebalance_backtest(
        _HoldA(symbols=["A"]), history, benchmark=("SPY", bench_closes),
        significance_runs=100, bootstrap_runs=100, monte_carlo_runs=100, seed=11)
    bench = report["benchmark"]
    assert bench["symbol"] == "SPY" and bench["source"] == "provider"
    assert "benchmark_fallback_equal_weight" not in report["warnings"]
    # The book IS the benchmark (up to first-day costs): beta ~ 1, r2 ~ 1.
    assert abs(bench["beta"] - 1.0) < 0.05
    assert bench["r2"] > 0.95
    assert abs(bench["alpha_daily"]) < 1e-3
    assert "2023" in bench["annual_returns"]
    # excess is computed from UNROUNDED totals; recomputing from the two
    # rounded payload numbers can differ by one rounding step.
    assert abs(bench["excess_return"]
               - (report["total_return"] - bench["benchmark_return"])) <= 1e-4
    # Deterministic blocks with runs > 0 and enough history.
    assert report["significance"]["method_sharpe"] == "sign_flip"
    assert report["significance"]["method_maxdd"] == "order_permutation"
    assert report["bootstrap"]["runs"] == 100
    assert report["monte_carlo"]["runs"] == 100
    json.dumps(report, allow_nan=False)


async def test_rebalance_stats_blocks_off_by_zero_runs() -> None:
    """runs=0 (the raw-caller default) -> blocks are None and no insufficient
    warnings fire; raw callers pay zero new cost."""
    report = await rebalance_backtest(_HoldA(symbols=["A"]), _two_symbol_history())
    assert report["significance"] is None
    assert report["bootstrap"] is None
    assert report["monte_carlo"] is None
    for warning in report["warnings"]:
        assert "insufficient_returns" not in warning
    # Untouched pre-existing keys are still there.
    for key in ("days_tested", "equity_curve", "annual_returns", "note", "sharpe"):
        assert key in report


async def test_rebalance_tiny_window_degrades_with_warnings_never_raises() -> None:
    """<20 daily returns -> significance/bootstrap/monte_carlo None plus
    explicit insufficient-data warnings; OLS all-None with its own warning.
    The ValueError of the raw Monte Carlo API must never escape."""
    history = _two_symbol_history()
    dates = sorted({b.start.date() for b in history["A"]})
    report = await rebalance_backtest(
        _HoldA(symbols=["A"]), history, start=dates[305],
        significance_runs=50, bootstrap_runs=50, monte_carlo_runs=50)
    assert report["days_tested"] < 20
    assert report["significance"] is None
    assert report["bootstrap"] is None
    assert report["monte_carlo"] is None
    for code in ("significance_insufficient_returns", "bootstrap_insufficient_returns",
                 "monte_carlo_insufficient_returns", "ols_insufficient_days"):
        assert code in report["warnings"]
    bench = report["benchmark"]
    assert bench["beta"] is None and bench["t_alpha"] is None
    json.dumps(report, allow_nan=False)
