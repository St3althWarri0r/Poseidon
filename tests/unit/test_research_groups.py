# tests/unit/test_research_groups.py
"""Quantile group-equity layering (design §4.4): per rebalance date, sort the
cross-section by (score, symbol) ascending into n_groups equal buckets, hold each
bucket to the NEXT rebalance date (return spans exactly one grid interval,
contiguous and non-overlapping), and compound each bucket's equal-weight return
from 1.0. A genuine-drift universe layers monotonically; thin data yields an
`insufficient` readout with None summaries and a reason, never a confident number
on too few periods or too narrow a cross-section."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.core.models import Bar
from poseidon.research.factors import Factor
from poseidon.research.groups import (
    _GROUP_MIN_PERIODS,
    _bucket_of,
    compute_group_equity,
)

# Last-visible close as the score: monotone in the level, trivially hand-checkable.
_LEVEL = Factor("level", lambda b: float(b[-1].close), min_bars=1)
# 5-bar trailing momentum: for a constant-drift series it ranks the SAME as the
# forward return, so the top bucket is genuinely the best.
_MOM = Factor("mom5", lambda b: float(b[-1].close) / float(b[-6].close) - 1.0, min_bars=6)


def _bars_daily(symbol: str, closes: list[float]) -> list[Bar]:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    out: list[Bar] = []
    for k, c in enumerate(closes):
        d = base + timedelta(days=k)
        out.append(Bar(symbol=symbol, open=Decimal(str(c)), high=Decimal(str(c)),
                       low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                       start=d, end=d, source="t"))
    return out


def _hist_daily(series: dict[str, list[float]]) -> dict[str, list[Bar]]:
    return {sym: _bars_daily(sym, closes) for sym, closes in series.items()}


def _drift_universe(n_symbols: int, length: int) -> dict[str, list[Bar]]:
    """n_symbols constant-drift series with strictly increasing drift, so trailing
    momentum ranks names identically to their forward return."""
    return _hist_daily({
        f"S{n:02d}": [100.0 * (1.0 + 0.001 * (n + 1)) ** k for k in range(length)]
        for n in range(n_symbols)
    })


# ------------------------------------------------------------------ bucketing


def test_bucket_sizes_differ_by_at_most_one() -> None:
    # g = i * n_groups // n partitions n sorted names into n_groups buckets whose
    # sizes differ by <= 1, and every bucket is non-empty when n >= n_groups.
    for n in range(5, 40):
        for n_groups in range(2, 6):
            if n < n_groups:
                continue
            sizes = [0] * n_groups
            for i in range(n):
                sizes[_bucket_of(i, n, n_groups)] += 1
            assert all(s >= 1 for s in sizes)            # no empty bucket
            assert max(sizes) - min(sizes) <= 1          # balanced to within one
            assert sum(sizes) == n                        # every name placed once


# ------------------------------------------------------- monotone layering


def test_drift_universe_layers_monotonically() -> None:
    # Strictly increasing drift => the top bucket holds the best names every period,
    # so bucket totals increase monotonically, long/short is positive, and the
    # rank correlation of (bucket index, total return) is a perfect +1.
    hist = _drift_universe(20, 100)
    res = compute_group_equity(_MOM, hist, n_groups=5, rebalance_every=5)
    assert not res.insufficient
    assert res.n_groups == 5
    assert res.breadth >= 15
    assert res.n_periods >= _GROUP_MIN_PERIODS
    assert all(res.total_return[g] < res.total_return[g + 1] for g in range(4))
    assert res.monotonic is True
    assert res.long_short is not None and res.long_short > 0.0
    assert res.mono_rho == pytest.approx(1.0)


# ------------------------------------------------------- hand-computed NAV


def test_hand_computed_two_period_nav() -> None:
    # 4 constant-drift names over 3 daily bars, rebalance_every=1 => exactly two
    # emitted hold intervals (the last grid date has no forward window). n_groups=2
    # splits {A,B} (low) vs {C,D} (high). Bucket returns per period:
    #   low  = mean(0.1, 0.2) = 0.15 ; high = mean(0.3, 0.4) = 0.35
    # NAV compounds from 1.0: low -> 1.15, 1.3225 ; high -> 1.35, 1.8225.
    hist = _hist_daily({
        "A": [10.0, 11.0, 12.1],   # +10% / period
        "B": [20.0, 24.0, 28.8],   # +20% / period
        "C": [30.0, 39.0, 50.7],   # +30% / period
        "D": [40.0, 56.0, 78.4],   # +40% / period
    })
    res = compute_group_equity(_LEVEL, hist, n_groups=2, rebalance_every=1, min_cross=2)
    assert res.n_periods == 2
    assert len(res.dates) == 2
    assert [len(curve) for curve in res.nav] == [3, 3]           # leading 1.0 + one per period
    assert res.nav[0] == pytest.approx([1.0, 1.15, 1.3225])
    assert res.nav[1] == pytest.approx([1.0, 1.35, 1.8225])
    assert res.total_return[0] == pytest.approx(0.3225)
    assert res.total_return[1] == pytest.approx(0.8225)


# ------------------------------------------ deterministic tie-break on symbol


def test_deterministic_under_symbol_reordering() -> None:
    # Two names carry an identical score at every date (a genuine tie). Sorting by
    # (score, symbol) breaks it deterministically, so shuffling the dict insertion
    # order leaves the NAV, totals, and dates byte-identical.
    series = {
        "AAA": [10.0 * 1.01 ** k for k in range(40)],
        "BBB": [10.0 * 1.01 ** k for k in range(40)],   # identical to AAA => a tie
        "CCC": [10.0 * 1.02 ** k for k in range(40)],
        "DDD": [10.0 * 1.03 ** k for k in range(40)],
    }
    a = compute_group_equity(_LEVEL, _hist_daily(series), n_groups=2,
                             rebalance_every=2, min_cross=2)
    reordered = {k: series[k] for k in ("DDD", "BBB", "CCC", "AAA")}
    b = compute_group_equity(_LEVEL, _hist_daily(reordered), n_groups=2,
                             rebalance_every=2, min_cross=2)
    assert a.nav == b.nav
    assert a.total_return == b.total_return
    assert a.dates == b.dates
    assert a.breadth == b.breadth


# ---------------------------- hold return spans exactly one grid interval


def test_hold_return_spans_exactly_one_grid_interval_mixed_calendar() -> None:
    # X trades every day; Y trades only on even days => the union calendar has gaps.
    # rebalance_every=2 lands each hold on the NEXT grid date, so a bucket's NAV
    # telescopes to the close ratio over the grid span (contiguous, non-overlapping)
    # rather than compounding any finer IC-horizon returns.
    base = datetime(2024, 1, 1, tzinfo=UTC)

    def _sparse(symbol: str, day_close: dict[int, float]) -> list[Bar]:
        out: list[Bar] = []
        for day, c in sorted(day_close.items()):
            d = base + timedelta(days=day)
            out.append(Bar(symbol=symbol, open=Decimal(str(c)), high=Decimal(str(c)),
                           low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                           start=d, end=d, source="t"))
        return out

    hist = {
        "X": _sparse("X", {0: 10.0, 1: 999.0, 2: 12.0, 3: 999.0, 4: 14.0}),
        "Y": _sparse("Y", {0: 100.0, 2: 110.0, 4: 132.0}),   # trades even days only
    }
    # Grid = union calendar [::2] = days 0, 2, 4. Two emitted holds: d0->d2, d2->d4.
    res = compute_group_equity(_LEVEL, hist, n_groups=2, rebalance_every=2, min_cross=2)
    assert res.n_periods == 2
    # X is always the lower score (bucket 0), Y the higher (bucket 1). Each bucket is
    # one name, so its terminal NAV telescopes to close_last_grid / close_first_grid,
    # proving the hold spanned one grid interval and skipped the off-grid day-1/3 bars.
    assert res.nav[0][-1] == pytest.approx(14.0 / 10.0)   # X: 12/10 * 14/12
    assert res.nav[1][-1] == pytest.approx(132.0 / 100.0)  # Y: 110/100 * 132/110


# ------------------------------------------------- thin-data insufficient path


def test_thin_periods_are_insufficient_with_none_summaries() -> None:
    # Only two hold intervals (< 12): the NAV data is still returned, but every
    # confident summary is None and the reason names the failing period count.
    hist = _hist_daily({
        "A": [10.0, 11.0, 12.1],
        "B": [20.0, 24.0, 28.8],
        "C": [30.0, 39.0, 50.7],
        "D": [40.0, 56.0, 78.4],
    })
    res = compute_group_equity(_LEVEL, hist, n_groups=2, rebalance_every=1, min_cross=2)
    assert res.insufficient is True
    assert res.reason == f"2 periods < {_GROUP_MIN_PERIODS}"
    assert res.long_short is None
    assert res.mono_rho is None
    assert res.monotonic is None
    assert res.nav and all(len(curve) == 3 for curve in res.nav)   # NAV still returned


def test_thin_breadth_is_insufficient_with_reason() -> None:
    # Enough periods but too narrow a cross-section (8 names < 3*5): breadth trips
    # the insufficiency flag with a breadth reason, summaries None.
    hist = _drift_universe(8, 100)
    res = compute_group_equity(_MOM, hist, n_groups=5, rebalance_every=5)
    assert res.n_periods >= _GROUP_MIN_PERIODS       # not a period shortfall
    assert res.insufficient is True
    assert res.reason == "breadth 8 < 15"
    assert res.long_short is None
    assert res.mono_rho is None
    assert res.monotonic is None


def test_bad_arguments_raise_value_error() -> None:
    hist = _drift_universe(20, 100)
    with pytest.raises(ValueError):
        compute_group_equity(_MOM, hist, n_groups=1, rebalance_every=5)
    with pytest.raises(ValueError):
        compute_group_equity(_MOM, hist, n_groups=5, rebalance_every=0)
