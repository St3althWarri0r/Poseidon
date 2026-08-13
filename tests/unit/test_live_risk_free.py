"""The LIVE risk-free rate behind the performance report (r3).

``analytics/performance.py`` always accepted ``risk_free_annual``, but its
caller passed nothing — so while the backtest surface was fixed, the live
report still reported rf=0 ratios. This pins the resolution and, more
importantly, the degrade: falling back to 0.0 silently restores exactly the
overstatement the rate exists to remove, so it must be logged, and the rate
actually used must travel in the payload beside the ratios it produced.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from poseidon.app import ApplicationKernel
from poseidon.core.config import BacktestEvalConfig
from poseidon.data.treasury import YieldCurveRow


class _Config:
    def __init__(self, rate: float | str) -> None:
        self.backtest = BacktestEvalConfig(risk_free_annual=rate)  # type: ignore[arg-type]


def _kernel(rate: float | str) -> ApplicationKernel:
    """A kernel shell: ``_live_risk_free`` reads only ``config.backtest``, so
    building the full application graph would test the fixture, not the code."""
    kernel = object.__new__(ApplicationKernel)
    kernel.config = _Config(rate)  # type: ignore[assignment]
    return kernel


async def test_explicit_rate_is_taken_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(**_kwargs: Any) -> Any:
        raise AssertionError("an explicit rate must not hit the network")

    monkeypatch.setattr("poseidon.data.treasury.fetch_yield_curve", explode)
    assert await _kernel(0.041)._live_risk_free() == pytest.approx(0.041)


async def test_auto_reads_the_treasury_three_month_yield(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from poseidon.core.clock import utc_now

    today = utc_now().date()
    rows = [YieldCurveRow(day=today, yields={"3M": 0.0387})]

    async def fake(*, year: int, **_kwargs: Any) -> list[YieldCurveRow]:
        return rows if year == today.year else []

    monkeypatch.setattr("poseidon.data.treasury.fetch_yield_curve", fake)
    assert await _kernel("auto")._live_risk_free() == pytest.approx(0.0387)


async def test_auto_falls_back_to_the_prior_year_before_the_first_print(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from poseidon.core.clock import utc_now

    today = utc_now().date()

    async def fake(*, year: int, **_kwargs: Any) -> list[YieldCurveRow]:
        if year == today.year - 1:
            return [YieldCurveRow(day=date(today.year - 1, 12, 31),
                                  yields={"3M": 0.0421})]
        return []  # this year has published nothing yet

    monkeypatch.setattr("poseidon.data.treasury.fetch_yield_curve", fake)
    assert await _kernel("auto")._live_risk_free() == pytest.approx(0.0421)


async def test_unreachable_treasury_degrades_to_zero_not_an_exception(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from poseidon.core.errors import DataError

    async def down(**_kwargs: Any) -> Any:
        raise DataError("Treasury yield-curve fetch failed")

    monkeypatch.setattr("poseidon.data.treasury.fetch_yield_curve", down)
    assert await _kernel("auto")._live_risk_free() == 0.0
