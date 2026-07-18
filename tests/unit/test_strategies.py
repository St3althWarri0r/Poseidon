"""Strategy screeners against synthetic data via the router shim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.core.config import StrategyConfig
from poseidon.core.errors import DataError
from poseidon.core.models import Bar
from poseidon.portfolio.state import PortfolioState
from poseidon.strategy.base import pct_return, realized_vol, sma
from poseidon.strategy.builtin.options_income import CashSecuredPutStrategy
from poseidon.strategy.builtin.reversion import MeanReversionStrategy
from poseidon.strategy.builtin.trend import BreakoutStrategy, MomentumStrategy
from poseidon.strategy.custom import CustomAlgorithm
from poseidon.strategy.engine import StrategyEngine


class BarsRouter:
    """Minimal router double serving canned bars."""

    def __init__(self, bars: dict[str, list[Bar]]) -> None:
        self._bars = bars

    async def bars(self, symbol: str, *, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        return self._bars[symbol.upper()][-limit:]


class RecordingBarsRouter:
    """Serves canned bars and records every symbol asked for (missing → [])."""

    def __init__(self, bars: dict[str, list[Bar]]) -> None:
        self._bars = bars
        self.requested: list[str] = []

    async def bars(self, symbol: str, *, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        self.requested.append(symbol.upper())
        return self._bars.get(symbol.upper(), [])[-limit:]


class QuoteRecordingRouter:
    """Records every quote request; always raises DataError (options scans
    then skip the symbol) so we can observe *which* symbols were considered."""

    def __init__(self) -> None:
        self.quoted: list[str] = []

    async def quote(self, symbol: str, *, allow_delayed: bool = False) -> object:
        self.quoted.append(symbol.upper())
        raise DataError(f"no data for {symbol}")


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


# --- TASK 5: extra_symbols additive widening through scan_all / Strategy.scan ---

_UPTREND = [100 * (1.006 ** i) for i in range(90)]
_DOWNTREND = [100 * (0.995 ** i) for i in range(90)]


async def test_scan_all_extra_symbols_widens_universe() -> None:
    # Configured name is a downtrend (no signal); the screener-supplied extra
    # is an uptrend — widening must scan it and surface its momentum signal.
    router = RecordingBarsRouter({"AAA": make_bars(_DOWNTREND), "BBB": make_bars(_UPTREND)})
    engine = StrategyEngine([StrategyConfig(name="momentum", symbols=["AAA"])], default_symbols=["AAA"])
    signals = await engine.scan_all(router, PortfolioState(), extra_symbols=["BBB"])  # type: ignore[arg-type]
    assert "BBB" in router.requested
    assert any(s.symbol == "BBB" and s.direction == "long" for s in signals)


async def test_scan_all_default_unchanged() -> None:
    # No extra_symbols ⇒ only the configured universe is scanned (byte-identical
    # set) and BBB never appears — the off-by-default / no-regression guarantee.
    router = RecordingBarsRouter({"AAA": make_bars(_DOWNTREND), "BBB": make_bars(_UPTREND)})
    engine = StrategyEngine([StrategyConfig(name="momentum", symbols=["AAA"])], default_symbols=["AAA"])
    signals = await engine.scan_all(router, PortfolioState())  # type: ignore[arg-type]
    assert set(router.requested) == {"AAA"}
    assert all(s.symbol != "BBB" for s in signals)


async def test_custom_algo_sees_extra_symbols() -> None:
    # A workshop algorithm emits one signal per ctx.symbol; widening must
    # expand ctx.symbols to include the screener extras.
    source = (
        "async def scan(ctx):\n"
        "    return [{'symbol': s, 'direction': 'long', 'strength': 0.5} for s in ctx.symbols]\n"
    )
    algo = CustomAlgorithm(algo_name="probe", source=source, symbols=["AAA"])
    router = RecordingBarsRouter({})
    without = await algo.scan(router, PortfolioState())  # type: ignore[arg-type]
    assert {s.symbol for s in without} == {"AAA"}
    with_extra = await algo.scan(router, PortfolioState(), extra_symbols=["bbb"])  # type: ignore[arg-type]
    assert {s.symbol for s in with_extra} == {"AAA", "BBB"}


async def test_options_income_ignores_extra_symbols() -> None:
    # options_income accepts the kwarg for Liskov but must NOT trade unheld
    # screened names — it only ever considers its configured symbols.
    router = QuoteRecordingRouter()
    csp = CashSecuredPutStrategy(symbols=["AAA"])
    signals = await csp.scan(router, PortfolioState(), extra_symbols=["BBB"])  # type: ignore[arg-type]
    assert signals == []
    assert "AAA" in router.quoted
    assert "BBB" not in router.quoted
