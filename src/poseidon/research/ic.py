"""Point-in-time IC/IR benchmarking. Pure — no I/O. A factor is handed only the
sliced past window, so look-ahead leakage is impossible at the factor boundary."""
from __future__ import annotations

import statistics
from datetime import date

from ..core.models import Bar


def visible_bars(bars: list[Bar], as_of: date) -> list[Bar]:
    """Bars with end.date() <= as_of (bars are chronological ascending)."""
    return [b for b in bars if b.end.date() <= as_of]


def forward_return(bars: list[Bar], as_of: date, horizon: int) -> float | None:
    """Return over the `horizon` bars AFTER the last bar visible at as_of. The
    factor's last bar (index i) is the base; bars[i+horizon] is the future label."""
    vis = visible_bars(bars, as_of)
    if not vis:
        return None
    i = len(vis) - 1
    if i + horizon >= len(bars):
        return None
    base = float(bars[i].close)
    if base == 0.0:
        return None
    return float(bars[i + horizon].close) / base - 1.0


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda k: xs[k])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0                 # average rank for ties (1-based)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation, or None for n<3 or a constant vector."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    try:
        return statistics.correlation(_ranks(xs), _ranks(ys))
    except statistics.StatisticsError:            # constant input -> undefined
        return None


def rebalance_dates(history: dict[str, list[Bar]], every: int) -> list[date]:
    """Every `every`-th distinct trading date across the whole universe."""
    dates = sorted({b.end.date() for bars in history.values() for b in bars})
    step = max(1, every)
    return dates[::step]
