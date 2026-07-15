# tests/unit/test_research_factors.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.factors import ALL_FACTORS, Factor  # noqa: F401


def _series(closes: list[float]) -> list[Bar]:
    out = []
    for k, c in enumerate(closes):
        d = datetime(2024, 1, 1, tzinfo=UTC)
        out.append(Bar(symbol="X", open=Decimal(str(c)), high=Decimal(str(c * 1.01)),
                       low=Decimal(str(c * 0.99)), close=Decimal(str(c)), volume=100 + k,
                       start=d, end=d, source="t"))
    return out


def test_all_factors_have_unique_names() -> None:
    names = [f.name for f in ALL_FACTORS]
    assert len(names) == len(set(names)) and len(names) >= 12


def test_factors_return_float_or_none() -> None:
    rising = _series([100 + i for i in range(300)])   # long rising series
    for f in ALL_FACTORS:
        v = f.fn(rising)
        assert v is None or isinstance(v, float)
        assert f.fn(_series([100.0])) is None          # too short -> None (min_bars)


def test_momentum_positive_on_uptrend() -> None:
    rising = _series([100 * (1.005 ** i) for i in range(300)])
    mom = next(f for f in ALL_FACTORS if f.name == "momentum_6m")
    assert mom.fn(rising) is not None and mom.fn(rising) > 0
