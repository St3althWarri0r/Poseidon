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
