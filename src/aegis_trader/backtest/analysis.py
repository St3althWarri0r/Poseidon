"""Monte Carlo, walk-forward, and stress analysis on backtest results."""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from datetime import timedelta

from ..core.models import Bar
from ..strategy.base import Strategy
from .engine import BacktestConfig, BacktestEngine, BacktestResult


@dataclass
class MonteCarloSummary:
    runs: int
    median_return: float
    p05_return: float
    p95_return: float
    median_max_drawdown: float
    p95_max_drawdown: float
    prob_loss: float


def monte_carlo(result: BacktestResult, *, runs: int = 1000,
                seed: int | None = None) -> MonteCarloSummary:
    """Bootstrap-resample the realized daily returns to estimate the
    distribution of outcomes and tail drawdowns."""
    rets = result.daily_returns
    if len(rets) < 20:
        raise ValueError("need at least 20 daily returns for Monte Carlo")
    rng = random.Random(seed)
    horizon = len(rets)
    finals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(runs):
        equity, peak, max_dd = 1.0, 1.0, 0.0
        for _ in range(horizon):
            equity *= 1 + rng.choice(rets)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
        finals.append(equity - 1)
        drawdowns.append(max_dd)
    finals.sort()
    drawdowns.sort()

    def pct(sorted_values: list[float], p: float) -> float:
        idx = min(int(p * len(sorted_values)), len(sorted_values) - 1)
        return sorted_values[idx]

    return MonteCarloSummary(
        runs=runs,
        median_return=round(statistics.median(finals), 4),
        p05_return=round(pct(finals, 0.05), 4),
        p95_return=round(pct(finals, 0.95), 4),
        median_max_drawdown=round(statistics.median(drawdowns), 4),
        p95_max_drawdown=round(pct(drawdowns, 0.95), 4),
        prob_loss=round(sum(1 for f in finals if f < 0) / runs, 3),
    )


async def walk_forward(strategy_factory, history: dict[str, list[Bar]], *,
                       folds: int = 4, config: BacktestConfig | None = None
                       ) -> list[dict[str, object]]:
    """Split the history into sequential folds and evaluate each out-of-sample
    segment with a strategy built fresh per fold (factory gets no data — the
    engine's visibility window prevents lookahead within the fold)."""
    all_dates = sorted({b.start.date() for bars in history.values() for b in bars})
    if len(all_dates) < folds * 40:
        raise ValueError("not enough history for the requested number of folds")
    engine = BacktestEngine(config)
    fold_size = len(all_dates) // folds
    reports: list[dict[str, object]] = []
    for i in range(folds):
        start = all_dates[i * fold_size]
        end = all_dates[min((i + 1) * fold_size, len(all_dates)) - 1]
        segment = {
            symbol: [b for b in bars if start <= b.start.date() <= end]
            for symbol, bars in history.items()
        }
        segment = {s: b for s, b in segment.items() if len(b) >= 30}
        if not segment:
            continue
        strategy: Strategy = strategy_factory()
        result = await engine.run(strategy, segment)
        reports.append({"fold": i + 1, "start": start.isoformat(),
                        "end": end.isoformat(), **result.summary()})
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
    """Overlay crisis shocks on the strategy's realized exposure profile and
    report the hypothetical drawdowns against the configured risk limits."""
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
    return dates[0].isoformat(), (dates[-1] + timedelta(days=0)).isoformat()
