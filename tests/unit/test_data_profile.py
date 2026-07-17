"""Instrument profile: capability, base contract, and reference-data model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from poseidon.core.models import InstrumentProfile
from poseidon.data.base import DataCapability, MarketDataProvider


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
