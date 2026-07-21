"""Correlation-matrix pins for ``analytics/correlation.py`` (rank 6 red-first).

Pure math first: golden pearson/spearman values computed by hand, tie-averaged
ranks, symmetry, honest ``None`` cells below ``min_overlap`` (or for constant
series), and per-PAIR date alignment across mixed 5-day/7-day calendars. Then
the async gatherer: crypto symbols batch through ``require=CRYPTO`` while
equities pass ``require=None``, and a book with fewer than two usable series
(or no covered pair) raises ``DataError`` instead of fabricating a matrix.

The rank/spearman helpers are deliberately LOCAL duplicates of research/ic.py
math — live code may never import ``poseidon.research`` (severance invariant,
enforced by test_research_isolation.py).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from poseidon.analytics.correlation import (
    _pearson,
    _ranks,
    _spearman,
    compute_correlation_matrix,
    gather_correlation_matrix,
)
from poseidon.core.errors import DataError
from poseidon.core.models import Bar
from poseidon.data.base import DataCapability


def _closes_from_returns(start: date, first: float, returns: list[float],
                         *, step_days: int = 1) -> list[tuple[date, float]]:
    """Build a (date, close) series whose consecutive returns are ``returns``."""
    out = [(start, first)]
    value = first
    for k, r in enumerate(returns, start=1):
        value *= 1.0 + r
        out.append((start + timedelta(days=k * step_days), value))
    return out


def _series(start: date, closes: list[float], *,
            skip_weekends: bool = False) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    d = start
    for c in closes:
        if skip_weekends:
            while d.weekday() >= 5:
                d += timedelta(days=1)
        out.append((d, c))
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ rank helpers


def test_ranks_average_ties() -> None:
    assert _ranks([3.0, 1.0, 4.0, 1.0, 5.0]) == [3.0, 1.5, 4.0, 1.5, 5.0]
    assert _ranks([2.0, 2.0, 2.0]) == [2.0, 2.0, 2.0]


def test_spearman_hand_value_with_ties() -> None:
    # ranks x: [1, 2.5, 2.5, 4]; ranks y: [1, 3, 2, 4] -> r = 4.5/sqrt(4.5*5).
    r = _spearman([1.0, 2.0, 2.0, 3.0], [10.0, 30.0, 20.0, 40.0])
    assert r == pytest.approx(math.sqrt(0.9), abs=1e-12)


def test_pearson_constant_series_is_none() -> None:
    assert _pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


# ------------------------------------------------------------------- pure matrix


def test_golden_pearson_matrix() -> None:
    # Hand-computed: returns a = [.01, -.02, .03, .00], b = [.02, -.01, .00, .01]
    # -> centered cov 3.0, var_a 13, var_b 5 (x1e4) -> r = 3/sqrt(65).
    d0 = date(2026, 1, 5)
    closes = {
        "AAA": _closes_from_returns(d0, 100.0, [0.01, -0.02, 0.03, 0.00]),
        "BBB": _closes_from_returns(d0, 50.0, [0.02, -0.01, 0.00, 0.01]),
    }
    report = compute_correlation_matrix(closes, method="pearson", min_overlap=3)
    expected = 3.0 / math.sqrt(65.0)
    assert report.symbols == ["AAA", "BBB"]
    assert report.method == "pearson"
    assert report.matrix[0][0] == 1.0 and report.matrix[1][1] == 1.0
    assert report.matrix[0][1] == pytest.approx(expected, abs=1e-9)
    assert report.matrix[1][0] == pytest.approx(expected, abs=1e-9)  # symmetric
    assert report.pair_observations[0][1] == 4
    assert report.window_start == d0
    assert report.window_end == d0 + timedelta(days=4)


def test_spearman_matrix_monotone_nonlinear_is_one() -> None:
    # b's returns are a STRICTLY increasing nonlinear function of a's (cubed):
    # rank correlation is exactly 1 while pearson is visibly below it — proves
    # the ranked method actually flows through the matrix path.
    d0 = date(2026, 1, 5)
    a = [0.01, 0.02, -0.03, 0.04, -0.01]
    closes = {
        "AAA": _closes_from_returns(d0, 100.0, a),
        "BBB": _closes_from_returns(d0, 50.0, [x * abs(x) * 10.0 for x in a]),
    }
    ranked = compute_correlation_matrix(closes, method="spearman", min_overlap=3)
    linear = compute_correlation_matrix(closes, method="pearson", min_overlap=3)
    assert ranked.matrix[0][1] == pytest.approx(1.0, abs=1e-9)
    linear_cell = linear.matrix[0][1]
    assert linear_cell is not None and linear_cell < 0.999


def test_below_min_overlap_cell_is_none_with_honest_observations() -> None:
    d0 = date(2026, 1, 5)
    closes = {
        "AAA": _closes_from_returns(d0, 100.0, [0.01, 0.02, -0.01]),
        "BBB": _closes_from_returns(d0, 50.0, [0.02, 0.01, 0.03]),
    }
    report = compute_correlation_matrix(closes, method="pearson", min_overlap=30)
    assert report.matrix[0][1] is None
    assert report.matrix[1][0] is None
    assert report.pair_observations[0][1] == 3  # measured, just not enough
    assert report.matrix[0][0] == 1.0  # diagonal stays 1.0 regardless


def test_constant_series_cell_is_none() -> None:
    d0 = date(2026, 1, 5)
    closes = {
        "AAA": _closes_from_returns(d0, 100.0, [0.01, 0.02, -0.01, 0.03]),
        "CCC": _series(d0, [50.0] * 5),  # flat: correlation undefined
    }
    report = compute_correlation_matrix(closes, method="pearson", min_overlap=3)
    assert report.matrix[0][1] is None
    assert report.pair_observations[0][1] == 4


def test_mixed_calendar_pair_aligns_on_shared_dates_only() -> None:
    # Equity trades weekdays; crypto trades every day. On SHARED dates the
    # crypto series is exactly 10x the equity series, so returns measured over
    # the intersected calendar are identical -> corr 1.0. The weekend closes
    # are wild — any implementation that computes returns on the crypto's own
    # calendar (or aligns by index) cannot produce 1.0.
    d0 = date(2026, 1, 5)  # a Monday
    equity_closes = [100.0, 102.0, 99.0, 103.0, 105.0, 104.0, 101.0, 106.0, 108.0, 107.0]
    equity = _series(d0, equity_closes, skip_weekends=True)
    crypto_map = {d: c * 10.0 for d, c in equity}
    crypto: list[tuple[date, float]] = []
    d = d0
    while d <= equity[-1][0]:
        crypto.append((d, crypto_map.get(d, 999_999.0 if d.weekday() == 5 else 1.0)))
        d += timedelta(days=1)
    report = compute_correlation_matrix(
        {"AAPL": equity, "BTC/USD": crypto}, method="pearson", min_overlap=5)
    i = report.symbols.index("AAPL")
    j = report.symbols.index("BTC/USD")
    assert report.matrix[i][j] == pytest.approx(1.0, abs=1e-9)
    assert report.pair_observations[i][j] == len(equity) - 1


def test_as_dict_shape_and_rounding() -> None:
    d0 = date(2026, 1, 5)
    closes = {
        "AAA": _closes_from_returns(d0, 100.0, [0.01, -0.02, 0.03, 0.00]),
        "BBB": _closes_from_returns(d0, 50.0, [0.02, -0.01, 0.00, 0.01]),
    }
    payload = compute_correlation_matrix(closes, method="pearson", min_overlap=3).as_dict()
    assert payload["symbols"] == ["AAA", "BBB"]
    assert payload["method"] == "pearson"
    assert payload["matrix"][0][0] == 1.0
    assert payload["matrix"][0][1] == round(3.0 / math.sqrt(65.0), 4)
    assert payload["pair_observations"][0][1] == 4
    assert payload["window_start"] == "2026-01-05"
    assert payload["window_end"] == "2026-01-09"


def test_unknown_method_rejected() -> None:
    d0 = date(2026, 1, 5)
    closes = {"AAA": _series(d0, [1.0, 2.0, 3.0])}
    with pytest.raises(ValueError, match="method"):
        compute_correlation_matrix(closes, method="kendall",  # type: ignore[arg-type]
                                   min_overlap=3)


# ---------------------------------------------------------------------- gather


def _bars_for(symbol: str, pairs: list[tuple[date, float]]) -> list[Bar]:
    out: list[Bar] = []
    for d, close in pairs:
        start = datetime.combine(d, time(0, 0), tzinfo=UTC)
        c = Decimal(str(close))
        out.append(Bar(symbol=symbol, open=c, high=c, low=c, close=c, volume=1000,
                       start=start, end=start + timedelta(days=1), source="fake"))
    return out


class _Router:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars = bars_by_symbol
        self.calls: list[dict[str, Any]] = []

    async def bars_multi(self, symbols: list[str], *, timeframe: str = "1d",
                         limit: int = 90, require: DataCapability | None = None,
                         concurrency: int | None = None) -> dict[str, list[Bar]]:
        self.calls.append({"symbols": list(symbols), "timeframe": timeframe,
                           "limit": limit, "require": require, "concurrency": concurrency})
        return {s: self._bars[s] for s in symbols if s in self._bars}


async def test_gather_splits_crypto_and_equity_batches() -> None:
    d0 = date(2026, 1, 5)
    series = _closes_from_returns(d0, 100.0, [0.01, -0.02, 0.03, 0.01, -0.01])
    router = _Router({
        "AAPL": _bars_for("AAPL", series),
        "MSFT": _bars_for("MSFT", [(d, c * 2.0) for d, c in series]),
        "BTC/USD": _bars_for("BTC/USD", [(d, c * 100.0) for d, c in series]),
        "ETH/USD": _bars_for("ETH/USD", [(d, c * 30.0) for d, c in series]),
    })
    report = await gather_correlation_matrix(
        router,  # type: ignore[arg-type]
        ["btc/usd", "AAPL", "ETH/USD", "msft"],
        window_days=40, method="pearson", min_overlap=3)
    assert report.symbols == ["AAPL", "BTC/USD", "ETH/USD", "MSFT"]
    by_require = {c["require"]: c for c in router.calls}
    assert set(by_require) == {None, DataCapability.CRYPTO}
    assert by_require[DataCapability.CRYPTO]["symbols"] == ["BTC/USD", "ETH/USD"]
    assert by_require[DataCapability.CRYPTO]["concurrency"] == 6
    assert by_require[None]["symbols"] == ["AAPL", "MSFT"]
    for call in router.calls:
        assert call["limit"] == 41  # window_days + 1 closes -> window_days returns
        assert call["timeframe"] == "1d"


async def test_gather_fewer_than_two_usable_symbols_raises() -> None:
    d0 = date(2026, 1, 5)
    router = _Router({"AAPL": _bars_for(
        "AAPL", _closes_from_returns(d0, 100.0, [0.01, 0.02]))})
    with pytest.raises(DataError, match="usable"):
        await gather_correlation_matrix(
            router,  # type: ignore[arg-type]
            ["AAPL", "MSFT"], window_days=40, method="pearson", min_overlap=3)


async def test_gather_no_covered_pair_raises() -> None:
    # Both symbols have history but the windows never overlap enough.
    router = _Router({
        "AAPL": _bars_for("AAPL", _closes_from_returns(
            date(2026, 1, 5), 100.0, [0.01, 0.02, -0.01])),
        "MSFT": _bars_for("MSFT", _closes_from_returns(
            date(2026, 3, 2), 200.0, [0.02, 0.01, 0.03])),
    })
    with pytest.raises(DataError, match="overlap"):
        await gather_correlation_matrix(
            router,  # type: ignore[arg-type]
            ["AAPL", "MSFT"], window_days=40, method="pearson", min_overlap=3)
