"""Monte Carlo, walk-forward, and stress analysis on backtest results."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import date

from ..core.models import Bar
from ..strategy.base import Strategy
from .engine import BacktestConfig, BacktestEngine, BacktestResult
from .rebalance import _MIN_WARMUP_DAYS, rebalance_backtest
from .stats import MonteCarloSummary, monte_carlo_returns


def monte_carlo(result: BacktestResult, *, runs: int = 1000,
                seed: int | None = None) -> MonteCarloSummary:
    """Bootstrap-resample the realized daily returns to estimate the
    distribution of outcomes and tail drawdowns. Thin delegate over
    :func:`poseidon.backtest.stats.monte_carlo_returns` (the computation
    moved there unchanged); this import path stays public."""
    return monte_carlo_returns(result.daily_returns, runs=runs, seed=seed)


async def walk_forward(strategy_factory: Callable[[], Strategy],
                       history: dict[str, list[Bar]], *,
                       folds: int = 4, warmup_days: int = 210,
                       config: BacktestConfig | None = None
                       ) -> list[dict[str, object]]:
    """Split the history into sequential folds and evaluate each out-of-sample
    segment with a strategy built fresh per fold (factory gets no data — the
    engine's visibility window prevents lookahead within the fold). Each fold's
    segment carries up to `warmup_days` of preceding bars so indicator lookbacks
    are warm at the fold start (the engine only trades from the fold's first
    day). Fold 1 gets only whatever history precedes it — callers wanting a
    fully warmed first fold should supply `warmup_days` of extra leading
    history."""
    all_dates = sorted({b.start.date() for bars in history.values() for b in bars})
    if len(all_dates) < folds * 40:
        raise ValueError("not enough history for the requested number of folds")
    engine = BacktestEngine(config)
    fold_size = len(all_dates) // folds
    reports: list[dict[str, object]] = []
    for i in range(folds):
        start = all_dates[i * fold_size]
        # Last fold absorbs the len(all_dates) % folds remainder so the most
        # recent trading days are evaluated.
        end = all_dates[-1] if i == folds - 1 else all_dates[(i + 1) * fold_size - 1]
        warmup_start = all_dates[max(0, i * fold_size - warmup_days)]
        segment = {
            symbol: [b for b in bars if warmup_start <= b.start.date() <= end]
            for symbol, bars in history.items()
        }
        # Count only in-fold bars toward the minimum — warmup bars alone must
        # not qualify a symbol for evaluation.
        segment = {s: b for s, b in segment.items()
                   if sum(1 for bar in b if bar.start.date() >= start) >= 30}
        if not segment:
            continue
        strategy: Strategy = strategy_factory()
        result = await engine.run(strategy, segment, start=start)
        reports.append({"fold": i + 1, "start": start.isoformat(),
                        "end": end.isoformat(), **result.summary()})
    return reports


async def walk_forward_rebalance(strategy_factory: Callable[[], Strategy],
                                 history: dict[str, list[Bar]], *,
                                 folds: int = 3,
                                 starting_cash: float = 100_000.0,
                                 slippage_pct: float = 0.0005,
                                 commission_per_trade: float = 0.0,
                                 start: date | None = None,
                                 end: date | None = None) -> list[dict[str, object]]:
    """Sequential out-of-sample folds for REBALANCE-mode algorithms: the
    evaluable region (after the shared 210-day warmup / requested window) is
    split into contiguous segments, each replayed with a FRESH strategy from
    the factory via ``rebalance_backtest`` (which itself enforces the
    anti-lookahead window and warmup). Mirrors the trade-engine walk_forward's
    40-day fold minimum; fewer than 2 viable folds -> [] (a one-fold "spread"
    is just the full backtest again). Fold entries carry point metrics only —
    no equity curves — so the payload stays small. Never raises for short
    history: a fold that cannot evaluate reports an ``insufficient_data``
    entry instead."""
    all_dates = sorted({b.start.date() for bars in history.values() for b in bars})
    if not all_dates:
        return []
    eval_from = _MIN_WARMUP_DAYS
    if start is not None:
        eval_from = max(_MIN_WARMUP_DAYS, bisect_left(all_dates, start))
    end_index = len(all_dates) - 1
    if end is not None:
        end_index = bisect_right(all_dates, end) - 1
    eval_days = end_index - eval_from + 1
    folds_effective = min(folds, eval_days // 40) if eval_days > 0 else 0
    if folds_effective < 2:
        return []
    fold_size = eval_days // folds_effective
    reports: list[dict[str, object]] = []
    for i in range(folds_effective):
        start_idx = eval_from + i * fold_size
        # Last fold absorbs the remainder so the most recent days are tested.
        end_idx = (end_index if i == folds_effective - 1
                   else eval_from + (i + 1) * fold_size - 1)
        fold_start, fold_end = all_dates[start_idx], all_dates[end_idx]
        entry: dict[str, object] = {"fold": i + 1, "start": fold_start.isoformat(),
                                    "end": fold_end.isoformat()}
        strategy: Strategy = strategy_factory()
        try:
            report = await rebalance_backtest(
                strategy, history, starting_cash=starting_cash,
                slippage_pct=slippage_pct, commission_per_trade=commission_per_trade,
                start=fold_start, end=fold_end)
        except ValueError:
            entry["error"] = "insufficient_data"
        else:
            for key in ("days_tested", "total_return", "cagr", "sharpe", "max_drawdown"):
                entry[key] = report[key]
        reports.append(entry)
    return reports


# Historical crisis-shaped shock scenarios applied to the equity curve's
# return stream: (name, one-day shock, subsequent daily drift, days of drift).
STRESS_SCENARIOS: list[tuple[str, float, float, int]] = [
    ("black_monday_1987", -0.20, -0.005, 5),
    ("gfc_oct_2008", -0.09, -0.01, 20),
    ("covid_mar_2020", -0.12, -0.02, 10),
    ("flash_crash_2010", -0.07, 0.002, 3),
    ("rate_shock", -0.04, -0.004, 15),
]


def stress_test(result: BacktestResult) -> list[dict[str, object]]:
    """Apply crisis-shaped shocks to the strategy's FINAL total equity, assuming
    full market exposure (beta 1, 100% net long), and report the hypothetical
    drawdown of each scenario. Note: this does not scale by the strategy's
    realized average exposure (a cash-heavy book is over-stated, a leveraged one
    under-stated) and does not compare against configured risk limits."""
    if not result.equity_curve:
        raise ValueError("empty backtest result")
    base_equity = result.equity_curve[-1][1]
    reports: list[dict[str, object]] = []
    for name, shock, drift, days in STRESS_SCENARIOS:
        equity = base_equity * (1 + shock)
        trough = equity
        for _ in range(days):
            equity *= 1 + drift
            trough = min(trough, equity)
        reports.append({
            "scenario": name,
            "immediate_shock": shock,
            "trough_equity": round(trough, 2),
            "total_drawdown": round((base_equity - trough) / base_equity, 4),
        })
    return reports


def replay_dates(history: dict[str, list[Bar]]) -> tuple[str, str]:
    dates = sorted({b.start.date() for bars in history.values() for b in bars})
    if not dates:
        return "", ""
    return dates[0].isoformat(), dates[-1].isoformat()
