"""Failover router behavior: priority, failover, staleness, penalties."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from poseidon.core.clock import FreshnessPolicy
from poseidon.core.enums import DataFreshness
from poseidon.core.errors import (
    AllProvidersFailedError,
    DataUnavailableError,
    ProviderError,
    StaleDataError,
)
from poseidon.core.models import InstrumentProfile
from poseidon.data.base import DataCapability, MarketDataProvider
from poseidon.data.router import DataRouter

from ..conftest import FakeProvider


@pytest.fixture
def policy() -> FreshnessPolicy:
    return FreshnessPolicy()


async def test_priority_order(policy: FreshnessPolicy) -> None:
    first = FakeProvider(name="first")
    second = FakeProvider(name="second")
    router = DataRouter([(second, 20), (first, 10)], policy)
    quote = await router.quote("AAPL")
    assert quote.source == "first"
    assert first.calls == 1 and second.calls == 0


async def test_failover_on_error(policy: FreshnessPolicy) -> None:
    broken = FakeProvider(name="broken", fail=True)
    backup = FakeProvider(name="backup")
    router = DataRouter([(broken, 10), (backup, 20)], policy)
    quote = await router.quote("AAPL")
    assert quote.source == "backup"


async def test_failed_provider_penalized(policy: FreshnessPolicy) -> None:
    broken = FakeProvider(name="broken", fail=True)
    backup = FakeProvider(name="backup")
    router = DataRouter([(broken, 10), (backup, 20)], policy)
    await router.quote("AAPL")
    broken_calls = broken.calls
    await router.quote("AAPL")  # penalized: not retried immediately
    assert broken.calls == broken_calls


async def test_all_fail_raises(policy: FreshnessPolicy) -> None:
    router = DataRouter([(FakeProvider(name="a", fail=True), 10),
                         (FakeProvider(name="b", fail=True), 20)], policy)
    with pytest.raises(AllProvidersFailedError):
        await router.quote("AAPL")


async def test_stale_data_rejected(policy: FreshnessPolicy) -> None:
    router = DataRouter([(FakeProvider(name="old", stale=True), 10)], policy)
    with pytest.raises(StaleDataError):
        await router.quote("AAPL", allow_delayed=True)


async def test_missing_capability(policy: FreshnessPolicy) -> None:
    router = DataRouter([(FakeProvider(name="a"), 10)], policy)
    with pytest.raises(DataUnavailableError):
        await router.news()


async def test_fresh_bars_pass(policy: FreshnessPolicy) -> None:
    # Normal daily bars (ending ~yesterday) are not stale.
    router = DataRouter([(FakeProvider(name="live", bars_count=90), 10)], policy)
    bars = await router.bars("AAPL", timeframe="1d", limit=60)
    assert len(bars) == 60


async def test_frozen_bar_feed_rejected(policy: FreshnessPolicy) -> None:
    # B4 regression: a feed whose newest daily bar is weeks old is frozen, and
    # must be rejected so stale history can never reach the AI or risk engine.
    router = DataRouter([(FakeProvider(name="frozen", bars_count=90, frozen_days=40), 10)], policy)
    with pytest.raises(StaleDataError):
        await router.bars("AAPL", timeframe="1d", limit=60)


async def test_last_resort_does_not_rehit_failed_provider(policy: FreshnessPolicy) -> None:
    # F1 regression: a provider that fails in pass 1 is penalized, but that must
    # not make it re-selected in the same request's last-resort pass 2.
    broken = FakeProvider(name="broken", fail=True)
    router = DataRouter([(broken, 10)], policy)
    with pytest.raises(AllProvidersFailedError):
        await router.quote("AAPL")
    assert broken.calls == 1  # tried once, not re-hit in the second pass


# -- crypto routing (DataCapability.CRYPTO + require=) -------------------------


async def test_crypto_quote_only_served_by_crypto_capable_provider(
    policy: FreshnessPolicy,
) -> None:
    # A crypto symbol must route ONLY to a CRYPTO-capable provider; the
    # higher-priority equity-only provider must be skipped, never 404'd.
    equity = FakeProvider(name="equity")  # QUOTES but NOT CRYPTO
    crypto = FakeProvider(name="crypto", crypto=True)
    router = DataRouter([(equity, 10), (crypto, 20)], policy)
    quote = await router.quote("BTC/USD")
    assert quote.source == "crypto"
    assert equity.calls == 0 and crypto.calls == 1


async def test_equity_only_provider_skipped_for_crypto(policy: FreshnessPolicy) -> None:
    # With no CRYPTO-capable provider configured, a crypto quote must fail
    # cleanly (no-data-no-trade) rather than hit the equity stocks endpoint.
    equity = FakeProvider(name="equity")
    router = DataRouter([(equity, 10)], policy)
    with pytest.raises(DataUnavailableError):
        await router.quote("BTC/USD")
    assert equity.calls == 0  # never called for a crypto symbol


async def test_equity_routing_unchanged_with_crypto_provider_present(
    policy: FreshnessPolicy,
) -> None:
    # An equity symbol still routes by priority (require=None); the presence of
    # a CRYPTO-capable provider must not perturb the equity path.
    equity = FakeProvider(name="equity")
    crypto = FakeProvider(name="crypto", crypto=True)
    router = DataRouter([(equity, 10), (crypto, 20)], policy)
    quote = await router.quote("AAPL")
    assert quote.source == "equity"
    assert equity.calls == 1 and crypto.calls == 0


async def test_crypto_bars_require_crypto_capable_provider(policy: FreshnessPolicy) -> None:
    equity = FakeProvider(name="equity", bars_count=90)
    crypto = FakeProvider(name="crypto", crypto=True, bars_count=90)
    router = DataRouter([(equity, 10), (crypto, 20)], policy)
    bars = await router.bars("BTC/USD", timeframe="1d", limit=60)
    assert bars and bars[0].source == "crypto"


async def test_all_crypto_providers_fail_raises(policy: FreshnessPolicy) -> None:
    router = DataRouter([(FakeProvider(name="crypto", crypto=True, fail=True), 10)], policy)
    with pytest.raises(AllProvidersFailedError):
        await router.quote("BTC/USD")


# -- crypto-aware freshness at the router gate --------------------------------


async def test_crypto_quote_30s_old_passes_order_gate() -> None:
    # A 30s-old crypto quote is REAL_TIME under the crypto window (default 60s)
    # and so passes the strict order gate (allow_delayed=False).
    policy = FreshnessPolicy(real_time_max_age=5.0, crypto_real_time_max_age=60.0)
    crypto = FakeProvider(name="crypto", crypto=True, age_seconds=30)
    router = DataRouter([(crypto, 10)], policy)
    quote = await router.quote("BTC/USD")
    assert quote.source == "crypto"
    assert quote.freshness is DataFreshness.REAL_TIME


async def test_equity_quote_30s_old_rejected_by_strict_gate() -> None:
    # Same 30s age, but an equity stays strict (5s real-time window): it grades
    # DELAYED and the order gate rejects it. Proves equities are unaffected.
    policy = FreshnessPolicy(real_time_max_age=5.0, crypto_real_time_max_age=60.0)
    equity = FakeProvider(name="equity", age_seconds=30)
    router = DataRouter([(equity, 10)], policy)
    with pytest.raises(StaleDataError):
        await router.quote("AAPL")


# -- profile routing (DataCapability.PROFILE + caching) -----------------------


class ProfileProvider(MarketDataProvider):
    """Profile-capable fake (conftest FakeProvider has no PROFILE)."""

    name = "profiler"

    def __init__(self, *, name: str = "profiler", fail: bool = False) -> None:
        super().__init__(api_key="test")
        self.name = name
        self._fail = fail
        self.profile_calls = 0

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.PROFILE})

    async def profile(self, symbol: str) -> InstrumentProfile:
        self.profile_calls += 1
        if self._fail:
            raise ProviderError(self.name, "simulated failure")
        return InstrumentProfile(symbol=symbol, name="Apple Inc",
                                 exchange="NASDAQ NMS - GLOBAL MARKET", currency="USD",
                                 as_of=datetime.now(UTC), source=self.name)


class _FakeClock:
    def __init__(self) -> None:
        self._now = itertools.count().__next__  # strictly increasing base
        self._offset = 0.0

    def advance(self, seconds: float) -> None:
        self._offset += seconds

    def monotonic(self) -> float:
        return self._now() * 1e-6 + self._offset


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr("time.monotonic", fake.monotonic)
    return fake


async def test_profile_cached_for_a_week(policy: FreshnessPolicy, clock: _FakeClock) -> None:
    provider = ProfileProvider()
    router = DataRouter([(provider, 10)], policy)
    first = await router.profile("aapl")
    assert first is not None and first.name == "Apple Inc"
    clock.advance(6 * 86400.0)
    second = await router.profile("AAPL")  # case-insensitive cache hit
    assert second == first
    assert provider.profile_calls == 1
    clock.advance(2 * 86400.0)  # past the 7-day TTL
    await router.profile("AAPL")
    assert provider.profile_calls == 2


async def test_profile_negative_cache_retries_hourly(policy: FreshnessPolicy,
                                                     clock: _FakeClock) -> None:
    provider = ProfileProvider(fail=True)
    router = DataRouter([(provider, 10)], policy)
    assert await router.profile("SPY") is None
    assert await router.profile("SPY") is None  # negative-cached, not re-hit
    assert provider.profile_calls == 1
    clock.advance(3601.0)  # past the 1-hour negative TTL (and the penalty box)
    assert await router.profile("SPY") is None
    assert provider.profile_calls == 2


async def test_profile_returns_none_when_all_providers_fail(policy: FreshnessPolicy) -> None:
    router = DataRouter([(ProfileProvider(name="a", fail=True), 10),
                         (ProfileProvider(name="b", fail=True), 20)], policy)
    assert await router.profile("AAPL") is None  # fail-open, never raises
    no_capability = DataRouter([(FakeProvider(name="quotes-only"), 10)], policy)
    assert await no_capability.profile("AAPL") is None
