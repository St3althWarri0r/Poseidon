"""Strategy screeners against synthetic data via the router shim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis_trader.core.models import Bar
from aegis_trader.portfolio.state import PortfolioState
from aegis_trader.strategy.base import pct_return, realized_vol, sma
from aegis_trader.strategy.builtin.reversion import MeanReversionStrategy
from aegis_trader.strategy.builtin.trend import BreakoutStrategy, MomentumStrategy


class BarsRouter:
    """Minimal router double serving canned bars."""

    def __init__(self, bars: dict[str, list[Bar]]) -> None:
        self._bars = bars

    async def bars(self, symbol: str, *, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        return self._bars[symbol.upper()][-limit:]


def make_bars(closes: list[float], volume: int = 500_000,
              last_volume: int | None = None) -> list[Bar]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    bars = []
    for i, close in enumerate(closes):
        vol = last_volume if (last_volume and i == len(closes) - 1) else volume
        day = start + timedelta(days=i)
        bars.append(Bar(symbol="TEST", open=Decimal(str(close * 0.995)),
                        high=Decimal(str(close * 1.01)), low=Decimal(str(close * 0.99)),
                        close=Decimal(str(close)), volume=vol, start=day, end=day, source="t"))
    return bars


def test_math_helpers() -> None:
    closes = [float(i) for i in range(1, 61)]
    assert sma(closes, 10) == sum(range(51, 61)) / 10
    assert pct_return(closes, 20) is not None
    assert realized_vol(closes, 20) is not None
    assert sma([1.0], 10) is None


async def test_momentum_fires_on_uptrend() -> None:
    closes = [100 * (1.006 ** i) for i in range(90)]
    router = BarsRouter({"TEST": make_bars(closes)})
    signals = await MomentumStrategy(symbols=["TEST"]).scan(router, PortfolioState())  # type: ignore[arg-type]
    assert signals and signals[0].direction == "long"
    assert signals[0].evidence["return_20d"] > 0.05


async def test_momentum_silent_on_downtrend() -> None:
    closes = [100 * (0.995 ** i) for i in range(90)]
    router = BarsRouter({"TEST": make_bars(closes)})
    signals = await MomentumStrategy(symbols=["TEST"]).scan(router, PortfolioState())  # type: ignore[arg-type]
    assert signals == []


async def test_breakout_needs_volume_confirmation() -> None:
    closes = [100.0] * 60 + [106.0]
    quiet = BarsRouter({"TEST": make_bars(closes)})
    signals = await BreakoutStrategy(symbols=["TEST"]).scan(quiet, PortfolioState())  # type: ignore[arg-type]
    assert signals == []  # breakout without volume: no signal
    loud = BarsRouter({"TEST": make_bars(closes, last_volume=2_000_000)})
    signals = await BreakoutStrategy(symbols=["TEST"]).scan(loud, PortfolioState())  # type: ignore[arg-type]
    assert signals and signals[0].evidence["volume_multiple"] >= 1.5


async def test_mean_reversion_only_in_uptrend() -> None:
    # Long uptrend then a sharp 3-day dump: oversold within an uptrend.
    closes = [100 * (1.004 ** i) for i in range(110)]
    closes += [closes[-1] * 0.97, closes[-1] * 0.94, closes[-1] * 0.90]
    router = BarsRouter({"TEST": make_bars(closes)})
    signals = await MeanReversionStrategy(symbols=["TEST"]).scan(router, PortfolioState())  # type: ignore[arg-type]
    assert signals and signals[0].direction == "long"
    assert signals[0].evidence["zscore_20d"] <= -2
