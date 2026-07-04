"""Failover router behavior: priority, failover, staleness, penalties."""

from __future__ import annotations

import pytest

from poseidon.core.clock import FreshnessPolicy
from poseidon.core.errors import AllProvidersFailedError, DataUnavailableError, StaleDataError
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
