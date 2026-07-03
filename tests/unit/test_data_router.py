"""Failover router behavior: priority, failover, staleness, penalties."""

from __future__ import annotations

import pytest

from aegis_trader.core.clock import FreshnessPolicy
from aegis_trader.core.errors import AllProvidersFailedError, DataUnavailableError, StaleDataError
from aegis_trader.data.router import DataRouter

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
