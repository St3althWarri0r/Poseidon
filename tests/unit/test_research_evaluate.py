# tests/unit/test_research_evaluate.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.core.models import Bar
from poseidon.research.factors import Factor
from poseidon.research.ic import evaluate_factor


def _hist(symbol_series: dict[str, list[float]]) -> dict[str, list[Bar]]:
    hist: dict[str, list[Bar]] = {}
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for sym, closes in symbol_series.items():
        bars = []
        for k, c in enumerate(closes):
            d = base + timedelta(days=k)
            bars.append(Bar(symbol=sym, open=Decimal(str(c)), high=Decimal(str(c)),
                            low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                            start=d, end=d, source="t"))
        hist[sym] = bars
    return hist


def test_factor_last_bar_is_never_beyond_its_rebalance_date() -> None:
    # The factor's latest visible bar must always be exactly a rebalance date and
    # never a future bar. If visible_bars leaked a bar dated after t, bars[-1] would
    # be a between/after-rebalance bar not in the rebalance set -> subset fails.
    from poseidon.research.ic import rebalance_dates
    seen: set = set()

    def probe(bars):
        seen.add(bars[-1].end.date())
        return float(len(bars))
    hist = _hist({s: [100 + i for i in range(40)] for s in ("A", "B", "C", "D", "E", "F")})
    rebs = set(rebalance_dates(hist, 5))
    evaluate_factor(Factor("probe", probe, min_bars=2), hist,
                    horizon=1, rebalance_every=5, horizons=[1])
    assert seen and seen <= rebs                    # never a bar dated after its rebalance date


def test_ic_plus_one_non_circular() -> None:
    # 6 symbols; each has a constant per-symbol drift => trailing momentum ranks the
    # SAME as forward return, WITHOUT the factor ever seeing the future.
    series = {}
    for n, drift in enumerate([0.001, 0.003, 0.005, 0.007, 0.009, 0.011]):
        series[f"S{n}"] = [100 * (1 + drift) ** k for k in range(60)]
    hist = _hist(series)
    mom = Factor("mom5", lambda b: b[-1].close.__float__() / float(b[-6].close) - 1.0, min_bars=6)
    res = evaluate_factor(mom, hist, horizon=5, rebalance_every=5, horizons=[5])
    assert res.ic_mean > 0.9                        # harness correlates past-signal with future
    neg = Factor("negmom", lambda b: -(float(b[-1].close) / float(b[-6].close) - 1.0), min_bars=6)
    assert evaluate_factor(neg, hist, horizon=5, rebalance_every=5, horizons=[5]).ic_mean < -0.9


def test_effective_n_formula() -> None:
    # The non-overlap count, tested directly (deterministic, not data-dependent).
    from poseidon.research.ic import _effective_n
    assert _effective_n(11, 20, 5) == 3     # stride ceil(20/5)=4 -> ceil(11/4)=3
    assert _effective_n(10, 5, 5) == 10     # rebalance == horizon -> no overlap
    assert _effective_n(12, 10, 5) == 6     # stride 2
    assert _effective_n(0, 20, 5) == 0


def test_evaluate_factor_rejects_non_positive_horizon_or_rebalance() -> None:
    # A non-positive horizon/rebalance_every/horizons entry would let forward_return's
    # `bars[i + horizon]` negative-index onto a REAL future bar — a silent look-ahead
    # leak. The CLI now accepts a user-supplied --horizon, so this must be a hard error.
    hist = _hist({s: [100 + i for i in range(30)] for s in ("A", "B", "C", "D", "E")})
    probe = Factor("probe", lambda b: float(len(b)), min_bars=2)
    with pytest.raises(ValueError):
        evaluate_factor(probe, hist, horizon=-1, rebalance_every=5, horizons=[1])
    with pytest.raises(ValueError):
        evaluate_factor(probe, hist, horizon=5, rebalance_every=0, horizons=[1])
    with pytest.raises(ValueError):
        evaluate_factor(probe, hist, horizon=5, rebalance_every=5, horizons=[1, 0])


def test_t_stat_uses_non_overlapping_n_eff() -> None:
    # IC must VARY across dates or ic_std=0 -> ir=0 -> t_stat=0 hides the bug. A per-
    # symbol drift ranks the cross-section; a symbol-phased wiggle makes each date's IC
    # high-but-imperfect and different, so ir != 0 and n_eff vs n_periods is testable.
    import math

    from poseidon.research.ic import _effective_n
    series = {f"S{n}": [100 * ((1 + 0.001 * n) ** i) * (1 + 0.02 * math.sin(0.3 * i + n))
                        for i in range(140)] for n in range(8)}
    hist = _hist(series)
    mom = Factor("m", lambda b: float(b[-1].close) / float(b[-11].close) - 1.0, min_bars=11)
    res = evaluate_factor(mom, hist, horizon=20, rebalance_every=5, horizons=[20])
    assert res.n_periods >= 2 and abs(res.ir) > 1e-9     # data produced a genuinely varying IC
    n_eff = _effective_n(res.n_periods, 20, 5)
    assert n_eff < res.n_periods                         # overlap present
    assert abs(res.t_stat - res.ir * math.sqrt(n_eff)) < 1e-9           # uses n_eff
    assert abs(res.t_stat - res.ir * math.sqrt(res.n_periods)) > 1e-9   # NOT the raw period count
