"""Rebalance-mode backtester for workshop algorithms.

Rotation models (Composer symphonies, tactical trees) don't trade entries
and exits — each day they declare a *target book*. This backtester replays
daily history through the exact ``CustomAlgorithm`` code that runs live,
reads the day's target from its signals (``evidence.target_weight``,
falling back to equal weight across ``long`` signals), and rebalances the
simulated book to it at that day's close with slippage and commission.
The anti-lookahead window from the core engine guarantees the algorithm
only ever sees bars up to the simulated day.

Honesty notes, same as the core engine: the AI judgment layer and the
risk engine are NOT simulated (live, they can only block or shrink what
the algorithm proposes), fills are daily-close approximations, and
delisted-ticker history you cannot fetch is survivorship bias you must
weigh yourself.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

import structlog

from ..core.models import Bar
from ..strategy.base import Strategy
from . import attribution, stats
from .engine import _HistoricalWindow, _RouterShim

log = structlog.get_logger(__name__)

_MIN_WARMUP_DAYS = 210  # algorithms routinely ask for 200d averages


async def rebalance_backtest(strategy: Strategy, history: dict[str, list[Bar]], *,
                             starting_cash: float = 100_000.0,
                             slippage_pct: float = 0.0005,
                             commission_per_trade: float = 0.0,
                             start: date | None = None,
                             end: date | None = None,
                             benchmark: tuple[str, dict[date, float]] | None = None,
                             risk_free_annual: float = 0.0,
                             factor_rows: list[Any] | None = None,
                             significance_runs: int = 0,
                             bootstrap_runs: int = 0,
                             monte_carlo_runs: int = 0,
                             seed: int = 42) -> dict[str, Any]:
    history = {s.upper(): bars for s, bars in history.items() if bars}
    all_dates = sorted({b.start.date() for bars in history.values() for b in bars})
    if len(all_dates) <= _MIN_WARMUP_DAYS + 20:
        raise ValueError(
            f"only {len(all_dates)} trading days of history — need at least "
            f"{_MIN_WARMUP_DAYS + 21} (a 200-day warmup plus a test window)"
        )
    if start is not None and end is not None and end <= start:
        raise ValueError("end date must be after start date")
    eval_from = _MIN_WARMUP_DAYS
    if start is not None:
        from bisect import bisect_left

        start_index = bisect_left(all_dates, start)
        if start_index < _MIN_WARMUP_DAYS:
            raise ValueError(
                f"only {start_index} trading days of history exist before {start} — "
                f"the algorithms need a {_MIN_WARMUP_DAYS}-day warmup; choose a later "
                "start or a symbol universe with deeper history"
            )
        eval_from = start_index
        if start_index >= len(all_dates):
            raise ValueError(f"no trading days on or after {start} in the fetched history")
    window = _HistoricalWindow(history)
    router = _RouterShim(window)

    closes_by_day: dict[str, dict[Any, float]] = {
        s: {b.start.date(): float(b.close) for b in bars} for s, bars in history.items()
    }
    from ..portfolio.state import PortfolioState

    cash = starting_cash
    holdings: dict[str, float] = {}  # symbol -> shares
    equity_curve: list[tuple[Any, float]] = []
    rebalances = trades = 0
    position_days: list[int] = []
    # Most recent close seen per symbol, updated as we walk days forward. Used
    # to mark held positions on days a symbol didn't print (holiday, halt, data
    # gap). Marking such a day at 0.0 would crater then rebound the equity
    # curve, fabricating drawdown and destroying the Sharpe/max-dd metrics.
    last_close: dict[str, float] = {}
    # Per-symbol P&L attribution (mark-to-mark accrual net of per-order
    # costs), held-day counts, and turnover bookkeeping.
    contrib: dict[str, float] = {}
    days_held: dict[str, int] = {}
    trading_costs = 0.0
    traded_value = 0.0

    def price(symbol: str, day: Any) -> float | None:
        return closes_by_day.get(symbol, {}).get(day)

    def mark(symbol: str) -> float:
        return last_close.get(symbol, 0.0)

    for day_index, day in enumerate(all_dates):
        # Yesterday's marks, snapshotted BEFORE today's cursor/mark advance —
        # the anchor for today's mark-to-mark contribution accrual.
        prev_mark = dict(last_close)
        for symbol, bars in history.items():
            cursor = window.cursor.get(symbol, 0)
            while cursor < len(bars) and bars[cursor].start.date() <= day:
                cursor += 1
            window.cursor[symbol] = cursor
            px_today = price(symbol, day)
            if px_today is not None:
                last_close[symbol] = px_today
        if day_index < eval_from:
            continue
        if end is not None and day > end:
            break

        # Accrue with the PRE-rebalance book: holdings mutate only in the
        # rebalance section below, so today's market move on the book is
        # exactly sum(qty * (mark - prev_mark)). Order costs are charged per
        # fill below; together this makes sum(contrib) == final - starting.
        for held, qty in holdings.items():
            contrib[held] = contrib.get(held, 0.0) + qty * (
                mark(held) - prev_mark.get(held, mark(held)))

        marked = cash + sum(qty * mark(s) for s, qty in holdings.items())
        try:
            signals = await strategy.scan(router, PortfolioState())  # type: ignore[arg-type]
        except Exception as exc:
            log.warning("backtest scan failed for a day; holding book", day=str(day), error=str(exc))
            for held in holdings:
                days_held[held] = days_held.get(held, 0) + 1
            equity_curve.append((day, marked))
            continue

        longs = [s for s in signals if s.direction == "long" and price(s.symbol, day)]
        weights: dict[str, float] = {}
        for s in longs:
            raw = s.evidence.get("target_weight")
            try:
                # Clamp: user-authored algorithms can emit any float. A negative
                # weight is dropped by the `weight > 0` filter below anyway, but if
                # left in the sum it deflates `total` and defeats the "never lever
                # up" normalization, silently levering the surviving longs past
                # 100% of equity.
                weights[s.symbol.upper()] = max(0.0, float(raw)) if raw is not None else 0.0
            except (TypeError, ValueError):
                weights[s.symbol.upper()] = 0.0
        total = sum(weights.values())
        if longs and total <= 0:
            weights = {s.symbol.upper(): 1.0 / len(longs) for s in longs}
            total = 1.0
        if total > 1.0:  # never lever up beyond fully invested
            weights = {k: v / total for k, v in weights.items()}

        # Equity locked in holdings that didn't print today (halt, gap,
        # delisting) cannot be sold — the loop below keeps those positions —
        # so it must not also fund new buys, or the book buys on cash it
        # doesn't have and ends up levered for free.
        locked = sum(qty * mark(s) for s, qty in holdings.items()
                     if price(s, day) is None)
        investable = max(marked - locked, 0.0)

        target_shares: dict[str, float] = {}
        for symbol, weight in weights.items():
            px = price(symbol, day)
            if px and weight > 0:
                target_shares[symbol] = investable * weight / px
        changed = False
        for symbol in set(holdings) | set(target_shares):
            px = price(symbol, day)
            if px is None:
                continue  # can't trade what didn't print; keep the position
            delta = target_shares.get(symbol, 0.0) - holdings.get(symbol, 0.0)
            if abs(delta * px) < max(marked * 0.001, 1.0):
                continue  # 10bp corridor: don't churn dust
            fill = px * (1 + slippage_pct) if delta > 0 else px * (1 - slippage_pct)
            cash -= delta * fill + commission_per_trade
            order_cost = abs(delta) * px * slippage_pct + commission_per_trade
            contrib[symbol] = contrib.get(symbol, 0.0) - order_cost
            trading_costs += order_cost
            traded_value += abs(delta) * px
            holdings[symbol] = holdings.get(symbol, 0.0) + delta
            if abs(holdings[symbol] * px) < 1.0:
                # The sub-$1 residual is dropped from the book's valuation
                # (dust); charge it to the symbol so contributions conserve.
                contrib[symbol] = contrib.get(symbol, 0.0) - holdings[symbol] * px
                holdings.pop(symbol, None)
            trades += 1
            changed = True
        if changed:
            rebalances += 1
        position_days.append(len(holdings))
        for held in holdings:
            days_held[held] = days_held.get(held, 0) + 1
        equity_curve.append((day, cash + sum(q * mark(s) for s, q in holdings.items())))

    if len(equity_curve) < 2:
        raise ValueError("backtest produced no evaluable days")

    # All statistics below are computed from the FULL internal values/rets —
    # never from the downsampled equity_curve payload.
    values = [v for _, v in equity_curve]
    curve_days = [d for d, _ in equity_curve]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    std = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0.0
    # Delegated rather than re-derived: this was a third inline copy of the
    # Sharpe formula, and the rf term has to reach every one of them.
    sharpe_value = stats.sharpe_ratio(rets, risk_free_annual=risk_free_annual)
    peak, max_dd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    by_year: dict[str, float] = {}
    year_start: dict[str, float] = {}
    prev: float | None = None
    for day, value in equity_curve:
        year = str(day.year)
        year_start.setdefault(year, prev if prev is not None else value)
        by_year[year] = value / year_start[year] - 1
        prev = value

    warnings: list[str] = []
    total_return_value = values[-1] / values[0] - 1 if values[0] > 0 else 0.0
    cagr_value = stats.safe_cagr(values[0], values[-1], len(values))
    if values[-1] <= 0 or total_return_value <= -0.99:
        # Never annualize a wipeout: a -99%+ (or negative-equity) outcome
        # reports CAGR -1.0 flat instead of a fabricated annualized rate (a
        # negative base to a fractional power is complex and round() crashes).
        warnings.append("wipeout_cagr_not_annualized")

    # Benchmark: provider closes when supplied, else the equal-weight
    # universe — the degrade is REPORTED (source field + warning), never
    # silent. Provider closes align to curve days with last-known-close
    # carry, the same gap discipline as the strategy marks.
    if benchmark is not None and benchmark[1]:
        bench_label, bench_closes = benchmark
        bench_source = "provider"
        bench_days = sorted(bench_closes)
        aligned: list[float] = []
        carry = 0.0
        idx = 0
        for day in curve_days:
            while idx < len(bench_days) and bench_days[idx] <= day:
                carry = bench_closes[bench_days[idx]]
                idx += 1
            aligned.append(carry)
        first_known = next((v for v in aligned if v > 0), 0.0)
        aligned = [v if v > 0 else first_known for v in aligned]
        bench_rets = [aligned[i] / aligned[i - 1] - 1
                      for i in range(1, len(aligned)) if aligned[i - 1] > 0]
    else:
        bench_label = f"EW({len(history)})"
        bench_source = "equal_weight_universe"
        warnings.append("benchmark_fallback_equal_weight")
        bench_rets = stats.equal_weight_returns(closes_by_day, curve_days)
    bench_values = [1.0]
    for r in bench_rets:
        bench_values.append(bench_values[-1] * (1 + r))
    bench_total = bench_values[-1] - 1
    bench_by_year: dict[str, float] = {}
    bench_year_start: dict[str, float] = {}
    prev_b: float | None = None
    for day, value in zip(curve_days, bench_values, strict=False):
        year = str(day.year)
        bench_year_start.setdefault(year, prev_b if prev_b is not None else value)
        bench_by_year[year] = value / bench_year_start[year] - 1
        prev_b = value

    ols = stats.ols_alpha_beta(rets, bench_rets)
    if isinstance(ols["n_days"], int) and ols["n_days"] <= 60:
        warnings.append("ols_insufficient_days")
    t_alpha = ols["t_alpha"]
    alpha_warning = ("|t(alpha)|<2 — alpha not statistically significant"
                     if isinstance(t_alpha, float) and 0 < abs(t_alpha) < 2 else None)
    information_ratio: float | None = None
    n_pairs = min(len(rets), len(bench_rets))
    if n_pairs >= 2:
        active = [rets[len(rets) - n_pairs + i] - bench_rets[len(bench_rets) - n_pairs + i]
                  for i in range(n_pairs)]
        mean_a = sum(active) / n_pairs
        std_a: float = (sum((a - mean_a) ** 2 for a in active) / (n_pairs - 1)) ** 0.5
        if std_a > 0:
            information_ratio = round(float(mean_a / std_a * 252 ** 0.5), 2)
    benchmark_block: dict[str, Any] = {
        "symbol": bench_label,
        "source": bench_source,
        "benchmark_return": round(bench_total, 4),
        "excess_return": round(total_return_value - bench_total, 4),
        "information_ratio": information_ratio,
        "alpha_daily": ols["alpha_daily"],
        "alpha_annual": ols["alpha_annual"],
        "beta": ols["beta"],
        "t_alpha": ols["t_alpha"],
        "r2": ols["r2"],
        "n_days": ols["n_days"],
        "alpha_warning": alpha_warning,
        "annual_returns": {y: round(r, 4) for y, r in sorted(bench_by_year.items())},
    }

    # Heavy statistics: each knob individually disabled by 0 (raw callers pay
    # zero new cost); short samples degrade to explicit None + warning — the
    # raw APIs' ValueError must never escape this report path.
    significance: dict[str, float | int | str] | None = None
    if significance_runs > 0:
        if len(rets) < 20:
            warnings.append("significance_insufficient_returns")
        else:
            significance = stats.permutation_significance(
                rets, runs=significance_runs, seed=seed)
    bootstrap: dict[str, Any] | None = None
    if bootstrap_runs > 0:
        if len(rets) < 20:
            warnings.append("bootstrap_insufficient_returns")
        else:
            bootstrap = stats.bootstrap_sharpe(
                rets, runs=bootstrap_runs, seed=seed,
                risk_free_annual=risk_free_annual)
    # Fama-French attribution: what survives market/size/value exposure. Rows
    # are passed IN rather than fetched — this function stays pure and offline.
    factor_block: dict[str, Any] | None = None
    if factor_rows:
        from .factor_model import attribute

        # Keyed by DATE: the factor series skips market holidays, so a
        # positional zip would pair different days and invent a correlation.
        by_day = {curve_days[i + 1]: rets[i] for i in range(len(rets))
                  if i + 1 < len(curve_days)}
        # NB: not named `attribution` — that is the imported module.
        factor_fit = attribute(by_day, factor_rows)
        if factor_fit is None:
            warnings.append("factor_attribution_insufficient_overlap")
        else:
            factor_block = factor_fit.as_dict()

    monte_carlo_block: dict[str, Any] | None = None
    if monte_carlo_runs > 0:
        if len(rets) < 20:
            warnings.append("monte_carlo_insufficient_returns")
        else:
            monte_carlo_block = asdict(
                stats.monte_carlo_returns(rets, runs=monte_carlo_runs, seed=seed))

    mean_equity = sum(values) / len(values) if values else 0.0
    turnover_gross = traded_value / mean_equity if mean_equity > 0 else 0.0
    turnover_annual = turnover_gross * 252 / max(len(values), 1)

    step = max(1, len(equity_curve) // 400)
    return {
        "days_tested": len(equity_curve),
        "warmup_days": _MIN_WARMUP_DAYS,
        "window": {"start": str(start) if start else "history start + warmup",
                   "end": str(end) if end else "latest bar"},
        "start": str(equity_curve[0][0]), "end": str(equity_curve[-1][0]),
        "starting_cash": starting_cash,
        "final_equity": round(values[-1], 2),
        "total_return": round(total_return_value, 4),
        "cagr": round(cagr_value, 4),
        "sharpe": round(sharpe_value, 2),
        "max_drawdown": round(max_dd, 4),
        "annual_returns": {y: round(r, 4) for y, r in sorted(by_year.items())},
        "rebalances": rebalances,
        "orders_simulated": trades,
        "avg_positions": round(sum(position_days) / len(position_days), 1) if position_days else 0,
        "annualized_volatility": round(float(std * 252 ** 0.5), 4),
        "sortino": round(stats.sortino(rets, risk_free_annual=risk_free_annual), 2),
        "factor_attribution": factor_block,
        "calmar": round(stats.calmar(cagr_value, max_dd), 2),
        "turnover_gross": round(turnover_gross, 4),
        "turnover_annual": round(turnover_annual, 4),
        "benchmark": benchmark_block,
        "significance": significance,
        "bootstrap": bootstrap,
        "monte_carlo": monte_carlo_block,
        "attribution": attribution.attribute_contributions(
            contrib, days_held, trading_costs, starting_cash),
        "health": stats.health_band(sharpe_value, max_dd),
        "warnings": sorted(set(warnings)),
        "equity_curve": [{"date": str(d), "equity": round(v, 2)}
                         for d, v in equity_curve[::step]],
        "note": ("Close-to-close simulation of the algorithm alone. Live, the AI and "
                 "the 20-rule risk engine sit between these signals and any order — "
                 "they can only reduce, never add, risk. Unfetchable delisted tickers "
                 "mean survivorship bias."),
    }
