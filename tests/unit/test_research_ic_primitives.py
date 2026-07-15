# tests/unit/test_research_ic_primitives.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.ic import forward_return, rebalance_dates, spearman, visible_bars


def _bars(closes: list[float], start_day: int = 1) -> list[Bar]:
    out = []
    for k, c in enumerate(closes):
        d = datetime(2026, 1, start_day + k, tzinfo=UTC)
        out.append(Bar(symbol="X", open=Decimal(str(c)), high=Decimal(str(c)),
                       low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                       start=d, end=d, source="t"))
    return out


def test_visible_bars_excludes_future() -> None:
    bars = _bars([1, 2, 3, 4, 5])          # days 1..5
    vis = visible_bars(bars, datetime(2026, 1, 3, tzinfo=UTC).date())
    assert [float(b.close) for b in vis] == [1, 2, 3]   # nothing after day 3


def test_forward_return_uses_future_label() -> None:
    bars = _bars([10, 11, 12, 13, 15])     # day1=10 ... day5=15
    # as_of day 2 (close 11), horizon 2 -> day 4 close 13 -> 13/11 - 1
    assert forward_return(bars, datetime(2026, 1, 2, tzinfo=UTC).date(), 2) == 13 / 11 - 1
    # horizon past the end -> None
    assert forward_return(bars, datetime(2026, 1, 4, tzinfo=UTC).date(), 5) is None


def test_spearman_monotonic_and_guards() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0      # perfectly rank-correlated
    assert round(spearman([1, 2, 3, 4], [40, 30, 20, 10]), 6) == -1.0
    assert spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None         # constant -> None
    assert spearman([1, 2], [1, 2]) is None                     # n < 3 -> None


def test_rebalance_dates_strides() -> None:
    hist = {"X": _bars([1, 2, 3, 4, 5, 6])}
    ds = rebalance_dates(hist, 2)
    assert ds[0].day == 1 and ds[1].day == 3 and ds[2].day == 5
