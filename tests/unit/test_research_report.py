# tests/unit/test_research_report.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.factors import ALL_FACTORS
from poseidon.research.report import run_report


def _hist(n_syms: int, n_days: int) -> dict[str, list[Bar]]:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    hist = {}
    for s in range(n_syms):
        bars = []
        for k in range(n_days):
            c = 100 + s + k * 0.1
            d = base + timedelta(days=k)
            bars.append(Bar(symbol=f"S{s}", open=Decimal(str(c)), high=Decimal(str(c)),
                            low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                            start=d, end=d, source="t"))
        hist[f"S{s}"] = bars
    return hist


def test_report_ranks_and_renders() -> None:
    rep = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5, horizons=[1, 5, 10])
    assert len(rep.results) == len(ALL_FACTORS)
    ts = [abs(r.t_stat) for r in rep.results]
    assert ts == sorted(ts, reverse=True)          # sorted by |t_stat| desc
    assert "IC" in rep.render() and "factor" in rep.render().lower()


def test_report_flags_thin_universe() -> None:
    rep = run_report(ALL_FACTORS, _hist(3, 300), horizon=5, rebalance_every=5, horizons=[5])
    assert rep.thin is True                          # 3 symbols is too thin to trust
    assert "thin" in rep.render().lower() or "noisy" in rep.render().lower()
