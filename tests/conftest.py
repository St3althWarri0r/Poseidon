"""Shared fixtures: fake market data providers and canned domain objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.core.clock import FreshnessPolicy
from poseidon.core.errors import ProviderError
from poseidon.core.models import Bar, Quote
from poseidon.data.base import DataCapability, MarketDataProvider
from poseidon.data.router import DataRouter


class FakeProvider(MarketDataProvider):
    """Configurable in-memory provider for router and risk tests."""

    name = "fake"

    def __init__(self, *, name: str = "fake", price: str = "100.00", fail: bool = False,
                 stale: bool = False, bars_count: int = 60, volume: int = 500_000,
                 frozen_days: int = 0, crypto: bool = False, age_seconds: float = 0.0) -> None:
        super().__init__(api_key="test")
        self.name = name
        self._price = Decimal(price)
        self._fail = fail
        self._stale = stale
        self._age_seconds = age_seconds  # quote as_of this many seconds in the past
        self._bars_count = bars_count
        self._volume = volume
        self._frozen_days = frozen_days  # shift all bars this many days into the past
        self._crypto = crypto  # also advertise DataCapability.CRYPTO
        self.calls = 0

    def capabilities(self) -> frozenset[DataCapability]:
        caps = {DataCapability.QUOTES, DataCapability.BARS,
                DataCapability.ECONOMIC_CALENDAR}
        if self._crypto:
            caps.add(DataCapability.CRYPTO)
        return frozenset(caps)

    async def quote(self, symbol: str) -> Quote:
        self.calls += 1
        if self._fail:
            raise ProviderError(self.name, "simulated failure")
        as_of = datetime.now(UTC) - (
            timedelta(hours=2) if self._stale else timedelta(seconds=self._age_seconds)
        )
        return Quote(
            symbol=symbol,
            bid=self._price - Decimal("0.05"),
            ask=self._price + Decimal("0.05"),
            last=self._price,
            as_of=as_of,
            source=self.name,
        )

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        if self._fail:
            raise ProviderError(self.name, "simulated failure")
        now = datetime.now(UTC)
        bars = []
        price = float(self._price)
        # Return the most recent `count` daily bars, ending yesterday — like a
        # real feed. (Offsetting by bars_count would leave the newest bar days
        # old whenever limit < bars_count, which the freshness gate rejects.)
        count = min(limit, self._bars_count)
        for i in range(count):
            day = now - timedelta(days=count - i + self._frozen_days)
            bars.append(
                Bar(symbol=symbol.upper(), open=Decimal(str(price)), high=Decimal(str(price * 1.01)),
                    low=Decimal(str(price * 0.99)), close=Decimal(str(price)),
                    volume=self._volume, start=day, end=day, source=self.name)
            )
        return bars

    async def economic_calendar(self, *, days_ahead: int = 7):
        return []


class FakeBatchProvider(MarketDataProvider):
    """In-memory provider that implements the batched ``bars_multi`` path.

    Configurable per-symbol behaviour lets the router tests exercise the batch
    contract without a network:
      * ``unimplemented=True`` — ``bars_multi`` raises ``NotImplementedError`` so
        the router falls back to the single-symbol degrade path.
      * ``fail=True`` — ``bars_multi`` raises ``ProviderError`` (whole-provider
        failure → the router penalizes and fails over / degrades).
      * ``absent`` — symbols simply omitted from the returned dict (mirrors a
        failed chunk / a name the feed has no data for: best-effort, absent).
      * ``frozen`` — symbols whose newest bar is weeks old (a stalled feed) so
        the router drops them via the ``_MAX_BAR_AGE`` guard.
      * ``unsound`` — symbols that carry one structurally-malformed bar so the
        router drops just that bar via ``_bar_is_sound``.
    ``multi_calls``/``single_calls`` count each path for cache/degrade asserts.
    """

    name = "fakebatch"

    def __init__(self, *, name: str = "fakebatch", unimplemented: bool = False,
                 fail: bool = False, bars_count: int = 90, volume: int = 1_000_000,
                 price: str = "100.00", absent: tuple[str, ...] = (),
                 frozen: tuple[str, ...] = (), unsound: tuple[str, ...] = ()) -> None:
        super().__init__(api_key="test")
        self.name = name
        self._unimplemented = unimplemented
        self._fail = fail
        self._bars_count = bars_count
        self._volume = volume
        self._price = float(Decimal(price))
        self._absent = {s.upper() for s in absent}
        self._frozen = {s.upper() for s in frozen}
        self._unsound = {s.upper() for s in unsound}
        self.multi_calls = 0
        self.single_calls = 0

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.QUOTES, DataCapability.BARS})

    def _bars_for(self, symbol: str, limit: int) -> list[Bar]:
        sym = symbol.upper()
        now = datetime.now(UTC)
        count = min(limit, self._bars_count)
        frozen_days = 40 if sym in self._frozen else 0
        bars: list[Bar] = []
        for i in range(count):
            day = now - timedelta(days=count - i + frozen_days)
            bars.append(
                Bar(symbol=sym, open=Decimal(str(self._price)),
                    high=Decimal(str(self._price * 1.01)), low=Decimal(str(self._price * 0.99)),
                    close=Decimal(str(self._price)), volume=self._volume,
                    start=day, end=day, source=self.name)
            )
        if sym in self._unsound and bars:
            # inject one structurally-broken bar (high < low) mid-series
            broken = bars[len(bars) // 2]
            bars[len(bars) // 2] = Bar(
                symbol=sym, open=broken.open, high=Decimal("1"), low=Decimal("99"),
                close=broken.close, volume=broken.volume,
                start=broken.start, end=broken.end, source=self.name,
            )
        return bars

    async def bars_multi(self, symbols: list[str], *, timeframe: str,
                         limit: int) -> dict[str, list[Bar]]:
        self.multi_calls += 1
        if self._unimplemented:
            raise NotImplementedError
        if self._fail:
            raise ProviderError(self.name, "simulated batch failure")
        return {
            s.upper(): self._bars_for(s, limit)
            for s in symbols if s.upper() not in self._absent
        }

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        self.single_calls += 1
        if self._fail or symbol.upper() in self._absent:
            raise ProviderError(self.name, f"simulated failure for {symbol}")
        return self._bars_for(symbol, limit)


@pytest.fixture
def fresh_policy() -> FreshnessPolicy:
    return FreshnessPolicy(real_time_max_age=5.0, delayed_max_age=900.0)


@pytest.fixture
def router(fresh_policy: FreshnessPolicy) -> DataRouter:
    return DataRouter([(FakeProvider(name="primary"), 10)], fresh_policy)


def make_quote(symbol: str = "AAPL", price: str = "100.00", *, spread: str = "0.10") -> Quote:
    p = Decimal(price)
    half = Decimal(spread) / 2
    return Quote(symbol=symbol, bid=p - half, ask=p + half, last=p,
                 as_of=datetime.now(UTC), source="test")
