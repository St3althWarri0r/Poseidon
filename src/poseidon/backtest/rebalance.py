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

from datetime import date
from typing import Any

import structlog

from ..core.models import Bar
from ..strategy.base import Strategy
from .engine import _HistoricalWindow, _RouterShim

log = structlog.get_logger(__name__)

_MIN_WARMUP_DAYS = 210  # algorithms routinely ask for 200d averages


async def rebalance_backtest(strategy: Strategy, history: dict[str, list[Bar]], *,
                             starting_cash: float = 100_000.0,
                             slippage_pct: float = 0.0005,
                             commission_per_trade: float = 0.0,
                             start: date | None = None,
                             end: date | None = None) -> dict[str, Any]:
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

    def price(symbol: str, day: Any) -> float | None:
        return closes_by_day.get(symbol, {}).get(day)

    def mark(symbol: str) -> float:
        return last_close.get(symbol, 0.0)

    for day_index, day in enumerate(all_dates):
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

        marked = cash + sum(qty * mark(s) for s, qty in holdings.items())
        try:
            signals = await strategy.scan(router, PortfolioState())  # type: ignore[arg-type]
        except Exception as exc:
            log.warning("backtest scan failed for a day; holding book", day=str(day), error=str(exc))
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
            holdings[symbol] = holdings.get(symbol, 0.0) + delta
            if abs(holdings[symbol] * px) < 1.0:
                holdings.pop(symbol, None)
            trades += 1
            changed = True
        if changed:
            rebalances += 1
        position_days.append(len(holdings))
        equity_curve.append((day, cash + sum(q * mark(s) for s, q in holdings.items())))

    if len(equity_curve) < 2:
        raise ValueError("backtest produced no evaluable days")

    values = [v for _, v in equity_curve]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    std = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0.0
    years = max(len(values) / 252, 1 / 252)
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

    step = max(1, len(equity_curve) // 400)
    return {
        "days_tested": len(equity_curve),
        "warmup_days": _MIN_WARMUP_DAYS,
        "window": {"start": str(start) if start else "history start + warmup",
                   "end": str(end) if end else "latest bar"},
        "start": str(equity_curve[0][0]), "end": str(equity_curve[-1][0]),
        "starting_cash": starting_cash,
        "final_equity": round(values[-1], 2),
        "total_return": round(values[-1] / values[0] - 1, 4),
        "cagr": round((values[-1] / values[0]) ** (1 / years) - 1, 4) if values[0] > 0 else 0.0,
        "sharpe": round(mean / std * 252 ** 0.5, 2) if std > 0 else 0.0,
        "max_drawdown": round(max_dd, 4),
        "annual_returns": {y: round(r, 4) for y, r in sorted(by_year.items())},
        "rebalances": rebalances,
        "orders_simulated": trades,
        "avg_positions": round(sum(position_days) / len(position_days), 1) if position_days else 0,
        "equity_curve": [{"date": str(d), "equity": round(v, 2)}
                         for d, v in equity_curve[::step]],
        "note": ("Close-to-close simulation of the algorithm alone. Live, the AI and "
                 "the 20-rule risk engine sit between these signals and any order — "
                 "they can only reduce, never add, risk. Unfetchable delisted tickers "
                 "mean survivorship bias."),
    }
