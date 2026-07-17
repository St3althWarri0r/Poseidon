"""Instrument profile: capability, base contract, and reference-data model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from poseidon.core.errors import ProviderError
from poseidon.core.models import InstrumentProfile
from poseidon.data.base import DataCapability, MarketDataProvider
from poseidon.data.providers.finnhub import FinnhubProvider


class _BareProvider(MarketDataProvider):
    name = "bare"

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset()


def test_profile_capability_exists() -> None:
    assert DataCapability.PROFILE == "profile"


async def test_base_provider_profile_raises_not_implemented() -> None:
    provider = _BareProvider(api_key="test")
    try:
        with pytest.raises(NotImplementedError):
            await provider.profile("AAPL")
    finally:
        await provider.close()


def test_instrument_profile_model_uppercases_and_stamps() -> None:
    as_of = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
    profile = InstrumentProfile(symbol="aapl", name="Apple Inc", as_of=as_of, source="finnhub")
    assert profile.symbol == "AAPL"
    assert profile.name == "Apple Inc"
    assert profile.exchange is None
    assert profile.currency is None
    assert profile.asset_type == "equity"
    assert profile.as_of == as_of
    assert profile.source == "finnhub"


def _finnhub_returning(payload: Any, calls: list[dict[str, Any]]) -> FinnhubProvider:
    provider = FinnhubProvider(api_key="test")

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None,
                            headers: dict[str, str] | None = None) -> Any:
        calls.append({"url": url, "params": dict(params or {})})
        return payload

    provider._get_json = fake_get_json  # type: ignore[method-assign]
    return provider


async def test_finnhub_profile_parses_profile2() -> None:
    calls: list[dict[str, Any]] = []
    provider = _finnhub_returning(
        {
            "name": "Apple Inc",
            "exchange": "NASDAQ NMS - GLOBAL MARKET",
            "currency": "USD",
            "finnhubIndustry": "Technology",
        },
        calls,
    )
    try:
        profile = await provider.profile("aapl")
    finally:
        await provider.close()
    assert calls[0]["url"].endswith("/stock/profile2")
    assert calls[0]["params"]["symbol"] == "AAPL"
    assert profile.symbol == "AAPL"
    assert profile.name == "Apple Inc"
    assert profile.exchange == "NASDAQ NMS - GLOBAL MARKET"
    assert profile.currency == "USD"
    assert profile.asset_type == "equity"
    assert profile.source == "finnhub"
    assert profile.as_of.tzinfo is not None


async def test_finnhub_profile_empty_raises_nonretryable() -> None:
    calls: list[dict[str, Any]] = []
    provider = _finnhub_returning({}, calls)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.profile("SPY")
    finally:
        await provider.close()
    assert excinfo.value.retryable is False
    assert "SPY" in str(excinfo.value)


async def test_finnhub_advertises_profile_capability() -> None:
    provider = FinnhubProvider(api_key="test")
    try:
        assert DataCapability.PROFILE in provider.capabilities()
    finally:
        await provider.close()
