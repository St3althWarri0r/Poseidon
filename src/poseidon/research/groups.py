"""Quantile group-equity layering (design §4.4). Pure — no I/O. On the shared
union-calendar rebalance grid, each date's cross-section is sorted by (score,
symbol) ascending into ``n_groups`` equal-weight buckets, and every bucket is held
to the NEXT rebalance date. That hold return spans exactly one grid interval
(``forward_return(bars, t, horizon=rebalance_every, calendar)`` lands on the next
grid date), so consecutive NAV periods are contiguous and non-overlapping
regardless of the IC horizon — compounding overlapping IC-horizon returns would be
wrong. Each bucket NAV compounds from 1.0.

Thin data is never dressed up as a confident readout: below ``_GROUP_MIN_PERIODS``
emitted intervals or a median cross-section narrower than ``_GROUP_MIN_PER_BUCKET *
n_groups`` names, the NAV data is still returned but ``long_short``/``mono_rho``/
``monotonic`` are None and ``reason`` explains which floor was missed.

Imports stay inside the research isolation allowlist: only ``.factors``, ``.ic``,
``..core.models``, and the stdlib."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

from ..core.models import Bar
from .factors import Factor
from .ic import forward_return, spearman, union_calendar, visible_bars

# Never surface a confident group readout on thin data (design §4.4).
_GROUP_MIN_PERIODS = 12      # emitted rebalance intervals required for a confident readout
_GROUP_MIN_PER_BUCKET = 3    # median cross-section must reach this many names PER bucket


@dataclass(frozen=True)
class GroupEquityResult:
    """Per-factor quantile layering. ``nav[g]`` is bucket g's equity curve compounded
    from a leading 1.0 (g=0 lowest score … n_groups-1 highest); ``total_return[g]``
    is ``nav[g][-1] - 1.0``. When ``insufficient`` the confident summaries
    (``long_short``/``mono_rho``/``monotonic``) are None — never a measured value on
    thin data — while ``nav``/``total_return`` are still populated. ``mono_rho`` is
    also None when ``n_groups < 3`` (Spearman is undefined for fewer than 3 buckets)."""

    factor: str
    n_groups: int
    n_periods: int                 # emitted rebalance intervals
    breadth: int                   # median per-date cross-section size (0 if none)
    dates: list[date]              # interval start dates, 1:1 with each NAV step
    nav: list[list[float]]         # nav[g] from 1.0; length n_periods + 1
    total_return: list[float]      # nav[g][-1] - 1.0
    long_short: float | None       # total_return[-1] - total_return[0]; None if insufficient
    mono_rho: float | None         # spearman(bucket idx, total_return); None if n_groups<3/insufficient
    monotonic: bool | None         # totals strictly increasing; None if insufficient
    insufficient: bool
    reason: str                    # "" or e.g. "6 periods < 12" / "breadth 8 < 15"


def _bucket_of(i: int, n: int, n_groups: int) -> int:
    """Bucket index of the i-th name of n sorted ascending by (score, symbol).
    ``g = i * n_groups // n`` gives bucket sizes at most one apart, and every bucket
    is non-empty when ``n >= n_groups``."""
    return i * n_groups // n


def compute_group_equity(factor: Factor, history: dict[str, list[Bar]], *,
                         n_groups: int = 5, rebalance_every: int,
                         min_cross: int = 5) -> GroupEquityResult:
    """Layer a factor into ``n_groups`` equal quantile buckets and compound each
    bucket's equal-weight hold-to-next-rebalance return from 1.0 (design §4.4)."""
    if n_groups < 2 or rebalance_every < 1 or min_cross < 1:
        raise ValueError(
            f"compute_group_equity requires n_groups >= 2, rebalance_every >= 1, and "
            f"min_cross >= 1 (got n_groups={n_groups}, rebalance_every={rebalance_every}, "
            f"min_cross={min_cross})"
        )
    calendar = union_calendar(history)
    grid = calendar[::max(1, rebalance_every)]
    # Every bucket must be non-empty: require at least n_groups names (and min_cross).
    need = max(min_cross, n_groups)
    emitted_dates: list[date] = []
    widths: list[int] = []
    bucket_rets: list[list[float]] = [[] for _ in range(n_groups)]   # per bucket, per period
    for t in grid:
        rows: list[tuple[float, str, float]] = []
        for symbol, bars in history.items():
            vis = visible_bars(bars, t)
            if len(vis) < factor.min_bars:
                continue
            score = factor.fn(vis)
            if score is None:
                continue
            # Hold to the NEXT grid date: horizon == rebalance_every, so the return
            # spans exactly one grid interval (contiguous, non-overlapping).
            ret = forward_return(bars, t, rebalance_every, calendar)
            if ret is None:
                continue
            rows.append((score, symbol, ret))
        if len(rows) < need:
            continue
        rows.sort(key=lambda row: (row[0], row[1]))   # (score, symbol) asc — deterministic tie-break
        n = len(rows)
        members: list[list[float]] = [[] for _ in range(n_groups)]
        for i, (_score, _symbol, ret) in enumerate(rows):
            members[_bucket_of(i, n, n_groups)].append(ret)
        for g in range(n_groups):
            bucket_rets[g].append(statistics.fmean(members[g]))   # equal-weight bucket return
        emitted_dates.append(t)
        widths.append(n)

    n_periods = len(emitted_dates)
    breadth = int(statistics.median(widths)) if widths else 0
    nav: list[list[float]] = []
    total_return: list[float] = []
    for g in range(n_groups):
        curve = [1.0]
        for ret in bucket_rets[g]:
            curve.append(curve[-1] * (1.0 + ret))
        nav.append(curve)
        total_return.append(curve[-1] - 1.0)

    # Thin-data guard: NAV is still returned, but confident summaries stay None.
    reason = ""
    if n_periods < _GROUP_MIN_PERIODS:
        reason = f"{n_periods} periods < {_GROUP_MIN_PERIODS}"
    elif breadth < _GROUP_MIN_PER_BUCKET * n_groups:
        reason = f"breadth {breadth} < {_GROUP_MIN_PER_BUCKET * n_groups}"
    if reason:
        return GroupEquityResult(
            factor=factor.name, n_groups=n_groups, n_periods=n_periods, breadth=breadth,
            dates=emitted_dates, nav=nav, total_return=total_return,
            long_short=None, mono_rho=None, monotonic=None, insufficient=True, reason=reason)

    long_short = total_return[-1] - total_return[0]
    monotonic = all(total_return[g] < total_return[g + 1] for g in range(n_groups - 1))
    # Spearman needs >= 3 buckets; below that the monotonicity rank is undefined.
    mono_rho = spearman([float(g) for g in range(n_groups)], total_return) if n_groups >= 3 else None
    return GroupEquityResult(
        factor=factor.name, n_groups=n_groups, n_periods=n_periods, breadth=breadth,
        dates=emitted_dates, nav=nav, total_return=total_return,
        long_short=long_short, mono_rho=mono_rho, monotonic=monotonic,
        insufficient=False, reason="")
