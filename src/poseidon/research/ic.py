"""Point-in-time IC/IR benchmarking. Pure — no I/O. A factor is handed only the
sliced past window, so look-ahead leakage is impossible at the factor boundary.

Mixed-calendar semantics: the rebalance grid AND every forward-return horizon are
measured on the shared union calendar of the universe's trading dates. In a mixed
universe (e.g. 7d/wk crypto alongside 5d/wk equities) this keeps every symbol's
forward window spanning the same calendar interval, and it makes the non-overlap
stride in `_effective_n` exact, because horizon and rebalance_every are counted in
the same calendar units for all symbols. A symbol that did not trade inside a
window (or whose history ends before the window closes) contributes no sample."""
from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from ..core.models import Bar
from .factors import Factor


def visible_bars(bars: list[Bar], as_of: date) -> list[Bar]:
    """Bars with end.date() <= as_of (bars are chronological ascending)."""
    return [b for b in bars if b.end.date() <= as_of]


def forward_return(bars: list[Bar], as_of: date, horizon: int,
                   calendar: list[date]) -> float | None:
    """Return from the last close visible at `as_of` to the last close visible at the
    date `horizon` steps after `as_of` on the shared `calendar` (sorted ascending).
    Measuring the horizon on the shared calendar — not per-symbol bar counts — keeps
    forward windows calendar-comparable across symbols with different trading weeks.
    None when the window extends past the calendar, the symbol's history ends before
    the window closes, or the symbol did not trade inside the window."""
    vis = visible_bars(bars, as_of)
    if not vis:
        return None
    k = bisect_right(calendar, as_of)                 # calendar[k-1] is the grid date <= as_of
    j = k - 1 + horizon
    if k == 0 or j >= len(calendar):
        return None
    t_fwd = calendar[j]
    if bars[-1].end.date() < t_fwd:                   # history ends inside the window
        return None
    i = len(vis) - 1
    fut = bisect_right(bars, t_fwd, key=lambda b: b.end.date()) - 1
    if fut <= i:                                      # no bar inside (as_of, t_fwd]
        return None
    base = float(bars[i].close)
    if base == 0.0:
        return None
    return float(bars[fut].close) / base - 1.0


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


def union_calendar(history: dict[str, list[Bar]]) -> list[date]:
    """Sorted union of every symbol's trading dates — the shared calendar on which
    both the rebalance grid and forward-return horizons are measured."""
    return sorted({b.end.date() for bars in history.values() for b in bars})


def rebalance_dates(history: dict[str, list[Bar]], every: int) -> list[date]:
    """Every `every`-th date of the shared union calendar."""
    return union_calendar(history)[::max(1, every)]


@dataclass(frozen=True)
class ICResult:
    """When n_periods == 0 the factor was never evaluated (insufficient history for
    min_bars/horizon) and the summary stats are 0.0 placeholders, not measured
    zeros; ic_by_horizon holds None for horizons with no samples. `breadth` is the
    median per-date cross-section size over emitted samples (0 when none)."""

    factor: str
    horizon: int
    ic_mean: float
    ic_std: float
    ir: float
    t_stat: float
    hit_rate: float
    n_periods: int
    ic_by_horizon: dict[int, float | None]
    breadth: int


def _ic_series(factor: Factor, history: dict[str, list[Bar]], dates: list[date],
               horizon: int, min_cross: int,
               calendar: list[date]) -> tuple[list[float], list[int]]:
    """IC per rebalance date plus each emitted sample's cross-section size."""
    series: list[float] = []
    widths: list[int] = []
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
            r = forward_return(bars, t, horizon, calendar)
            if r is None:
                continue
            vals.append(v)
            fwds.append(r)
        if len(vals) >= min_cross:
            ic = spearman(vals, fwds)
            if ic is not None:
                series.append(ic)
                widths.append(len(vals))
    return series, widths


def _effective_n(n_periods: int, horizon: int, rebalance_every: int) -> int:
    """Count of NON-OVERLAPPING forward windows among n_periods rebalances. When
    rebalance_every < horizon the windows overlap and the IC series autocorrelates, so
    the t-stat must use independent observations, not the raw period count. Both
    arguments are counted in shared-union-calendar dates, so the stride is exact for
    every symbol regardless of its own trading week."""
    if n_periods <= 0:
        return 0
    stride = max(1, math.ceil(horizon / max(1, rebalance_every)))
    return math.ceil(n_periods / stride)


def evaluate_factor(factor: Factor, history: dict[str, list[Bar]], *, horizon: int,
                    rebalance_every: int, horizons: list[int],
                    min_cross: int = 5) -> ICResult:
    # Defense in depth: a non-positive horizon/rebalance_every/horizons entry would let
    # forward_return index onto a REAL future bar — a silent look-ahead leak, not a
    # crash. The CLI now passes through a user-supplied --horizon, so this can no
    # longer be assumed to always come from trusted call sites.
    if horizon < 1 or rebalance_every < 1 or any(h < 1 for h in horizons):
        raise ValueError(
            f"evaluate_factor requires horizon >= 1, rebalance_every >= 1, and all "
            f"horizons >= 1 (got horizon={horizon}, rebalance_every={rebalance_every}, "
            f"horizons={horizons})"
        )
    calendar = union_calendar(history)
    dates = calendar[::max(1, rebalance_every)]
    ic, widths = _ic_series(factor, history, dates, horizon, min_cross, calendar)
    n = len(ic)
    ic_mean = statistics.fmean(ic) if ic else 0.0
    ic_std = statistics.stdev(ic) if n >= 2 else 0.0
    ir = ic_mean / ic_std if ic_std else 0.0
    n_eff = _effective_n(n, horizon, rebalance_every)   # independent (non-overlapping) samples
    t_stat = ir * math.sqrt(n_eff) if n_eff else 0.0
    hit_rate = sum(1 for x in ic if x > 0) / n if n else 0.0
    by_h: dict[int, float | None] = {}
    for h in horizons:
        s, _ = _ic_series(factor, history, dates, h, min_cross, calendar)
        by_h[h] = statistics.fmean(s) if s else None
    breadth = int(statistics.median(widths)) if widths else 0
    return ICResult(factor.name, horizon, ic_mean, ic_std, ir, t_stat, hit_rate, n,
                    by_h, breadth)
