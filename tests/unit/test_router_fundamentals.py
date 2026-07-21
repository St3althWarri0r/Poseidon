# tests/unit/test_router_fundamentals.py
"""DataRouter fundamentals/filings/insider routing (r2-wave2 rank 4).

Pins: priority failover with skip-without-penalty for non-retryable errors,
the positive-only TTL caches (successes cached, failures never), the crypto
short-circuit (zero provider calls), the empty-insider-list-is-success
contract, and DataUnavailableError when no provider advertises the capability.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poseidon.core.clock import FreshnessPolicy
from poseidon.core.errors import (
    AllProvidersFailedError,
    DataUnavailableError,
    ProviderError,
)
from poseidon.core.models import Filing, FundamentalsReport, InsiderTransaction
from poseidon.data.base import DataCapability, MarketDataProvider
from poseidon.data.router import DataRouter

_AS_OF = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class _FundamentalsProvider(MarketDataProvider):
    """Configurable fundamentals/filings/insider fake."""

    def __init__(self, name: str, *, fail: str | None = None,
                 insider_rows: int = 2) -> None:
        super().__init__(api_key="")
        self.name = name
        self._fail = fail  # None | "retryable" | "non_retryable"
        self._insider_rows = insider_rows
        self.calls = 0

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.FUNDAMENTALS, DataCapability.FILINGS,
                          DataCapability.INSIDER})

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self._fail == "retryable":
            raise ProviderError(self.name, "boom")
        if self._fail == "non_retryable":
            raise ProviderError(self.name, "no fundamentals for X", retryable=False)

    async def fundamentals(self, symbol: str) -> FundamentalsReport:
        self._maybe_fail()
        return FundamentalsReport(symbol=symbol, overview=None, statements=[],
                                  as_of=_AS_OF, source=self.name)

    async def filings(self, symbol: str, *, limit: int = 10) -> list[Filing]:
        self._maybe_fail()
        return [Filing(symbol=symbol, form="10-K", filed=_AS_OF.date(),
                       accession=f"{self.name}-{i}", as_of=_AS_OF, source=self.name)
                for i in range(min(limit, 3))]

    async def insider_transactions(self, symbol: str, *,
                                   limit: int = 20) -> list[InsiderTransaction]:
        self._maybe_fail()
        return [InsiderTransaction(symbol=symbol, name=f"insider {i}",
                                   shares_changed=Decimal("-10"),
                                   as_of=_AS_OF, source=self.name)
                for i in range(min(limit, self._insider_rows))]


def _router(*providers: _FundamentalsProvider) -> DataRouter:
    return DataRouter([(p, (i + 1) * 10) for i, p in enumerate(providers)],
                      FreshnessPolicy())


# ----------------------------------------------------------- failover


async def test_priority_failover_non_retryable_skips_without_penalty() -> None:
    primary = _FundamentalsProvider("edgar", fail="non_retryable")
    backup = _FundamentalsProvider("av")
    router = _router(primary, backup)

    report = await router.fundamentals("AAPL")
    assert report.source == "av"
    # skip-without-penalty: the healthy-but-unable provider is NOT penalty-boxed
    status = {s["name"]: s for s in router.provider_status()}
    assert status["edgar"]["available"] is True
    assert status["edgar"]["consecutive_failures"] == 0


async def test_retryable_failure_penalizes_and_fails_over() -> None:
    primary = _FundamentalsProvider("edgar", fail="retryable")
    backup = _FundamentalsProvider("av")
    report = await _router(primary, backup).fundamentals("AAPL")
    assert report.source == "av"


async def test_all_fail_raises() -> None:
    router = _router(_FundamentalsProvider("a", fail="retryable"),
                     _FundamentalsProvider("b", fail="retryable"))
    with pytest.raises(AllProvidersFailedError):
        await router.fundamentals("AAPL")


async def test_no_capable_provider_names_the_capability() -> None:
    # Providers exist but none advertises the fundamentals-family capabilities.
    from tests.conftest import FakeProvider

    router = DataRouter([(FakeProvider(name="quotes_only"), 10)], FreshnessPolicy())
    with pytest.raises(DataUnavailableError, match="fundamentals"):
        await router.fundamentals("AAPL")
    with pytest.raises(DataUnavailableError, match="filings"):
        await router.filings("AAPL")
    with pytest.raises(DataUnavailableError, match="insider"):
        await router.insider_transactions("AAPL")


# ----------------------------------------------------------- caching


async def test_fundamentals_positive_cache_within_ttl() -> None:
    provider = _FundamentalsProvider("edgar")
    router = _router(provider)
    first = await router.fundamentals("AAPL")
    second = await router.fundamentals("aapl")  # upper-symbol cache key
    assert provider.calls == 1
    assert second == first


async def test_failure_is_never_cached() -> None:
    provider = _FundamentalsProvider("edgar", fail="retryable")
    router = _router(provider)
    with pytest.raises(AllProvidersFailedError):
        await router.fundamentals("AAPL")
    provider._fail = None
    # The failure was not cached: the retry reaches the provider and succeeds.
    # (The penalty box may defer it to the last-resort pass, but it IS called.)
    report = await router.fundamentals("AAPL")
    assert report.source == "edgar" and provider.calls == 2


async def test_filings_and_insider_cached_per_symbol() -> None:
    provider = _FundamentalsProvider("edgar")
    router = _router(provider)
    await router.filings("AAPL", limit=3)
    await router.filings("AAPL", limit=3)
    assert provider.calls == 1
    await router.insider_transactions("AAPL", limit=5)
    await router.insider_transactions("AAPL", limit=5)
    assert provider.calls == 2


async def test_cached_list_serves_smaller_limit_but_refetches_larger() -> None:
    provider = _FundamentalsProvider("edgar", insider_rows=20)
    router = _router(provider)
    full = await router.insider_transactions("AAPL", limit=10)
    assert len(full) == 10 and provider.calls == 1
    smaller = await router.insider_transactions("AAPL", limit=4)
    assert len(smaller) == 4 and provider.calls == 1  # sliced from cache
    larger = await router.insider_transactions("AAPL", limit=15)
    assert len(larger) == 15 and provider.calls == 2  # cache can't serve more


async def test_empty_insider_list_is_success_and_cached() -> None:
    provider = _FundamentalsProvider("edgar", insider_rows=0)
    router = _router(provider)
    assert await router.insider_transactions("AAPL") == []
    assert await router.insider_transactions("AAPL") == []
    assert provider.calls == 1  # "none reported" is a cacheable SUCCESS


# ----------------------------------------------------------- crypto short-circuit


async def test_crypto_symbol_short_circuits_with_zero_provider_calls() -> None:
    provider = _FundamentalsProvider("edgar")
    router = _router(provider)
    with pytest.raises(DataUnavailableError, match="crypto"):
        await router.fundamentals("BTC/USD")
    with pytest.raises(DataUnavailableError, match="crypto"):
        await router.filings("BTC/USD")
    with pytest.raises(DataUnavailableError, match="crypto"):
        await router.insider_transactions("ETH/USD")
    assert provider.calls == 0
