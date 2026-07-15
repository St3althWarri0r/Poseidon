"""Point-in-time IC/IR benchmarking. Pure — no I/O. A factor is handed only the
sliced past window, so look-ahead leakage is impossible at the factor boundary."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date

from ..core.models import Bar
from .factors import Factor


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


@dataclass(frozen=True)
class ICResult:
    factor: str
    horizon: int
    ic_mean: float
    ic_std: float
    ir: float
    t_stat: float
    hit_rate: float
    n_periods: int
    ic_by_horizon: dict[int, float]


def _ic_series(factor: Factor, history: dict[str, list[Bar]], dates: list[date],
               horizon: int, min_cross: int) -> list[float]:
    series: list[float] = []
    for t in dates:
        vals: list[float] = []
        fwds: list[float] = []
        for bars in history.values():
            vis = visible_bars(bars, t)
            if len(vis) < factor.min_bars:
                continue
            v = factor.fn(vis)
            if v is None:
                continue
            r = forward_return(bars, t, horizon)
            if r is None:
                continue
            vals.append(v)
            fwds.append(r)
        if len(vals) >= min_cross:
            ic = spearman(vals, fwds)
            if ic is not None:
                series.append(ic)
    return series


def _effective_n(n_periods: int, horizon: int, rebalance_every: int) -> int:
    """Count of NON-OVERLAPPING forward windows among n_periods rebalances. When
    rebalance_every < horizon the windows overlap and the IC series autocorrelates, so
    the t-stat must use independent observations, not the raw period count."""
    if n_periods <= 0:
        return 0
    stride = max(1, math.ceil(horizon / max(1, rebalance_every)))
    return math.ceil(n_periods / stride)


def evaluate_factor(factor: Factor, history: dict[str, list[Bar]], *, horizon: int,
                    rebalance_every: int, horizons: list[int],
                    min_cross: int = 5) -> ICResult:
    dates = rebalance_dates(history, rebalance_every)
    ic = _ic_series(factor, history, dates, horizon, min_cross)
    n = len(ic)
    ic_mean = statistics.fmean(ic) if ic else 0.0
    ic_std = statistics.stdev(ic) if n >= 2 else 0.0
    ir = ic_mean / ic_std if ic_std else 0.0
    n_eff = _effective_n(n, horizon, rebalance_every)   # independent (non-overlapping) samples
    t_stat = ir * math.sqrt(n_eff) if n_eff else 0.0
    hit_rate = sum(1 for x in ic if x > 0) / n if n else 0.0
    by_h: dict[int, float] = {}
    for h in horizons:
        s = _ic_series(factor, history, dates, h, min_cross)
        by_h[h] = statistics.fmean(s) if s else 0.0
    return ICResult(factor.name, horizon, ic_mean, ic_std, ir, t_stat, hit_rate, n, by_h)
