"""Alpaca crypto data path (Task 3). No network.

Drives ``AlpacaDataProvider.quote()``/``bars()`` over an ``httpx.MockTransport``
so the real ``_get``/``_get_json``/``_decode`` + JSON parse runs, exactly like
``test_p1_twelvedata.py``. The crypto data API (``v1beta3``) is multi-symbol and
keyed by symbol — ``{"quotes": {"BTC/USD": {...}}}`` / ``{"bars": {"BTC/USD":
[...]}}`` — a different shape from the per-symbol stocks endpoint, so these tests
pin the crypto parse AND confirm the equity path is untouched.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from poseidon.core.errors import ProviderError, UnsupportedSymbolError
from poseidon.data.base import DataCapability
from poseidon.data.providers.alpaca_data import AlpacaDataProvider


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> AlpacaDataProvider:
    provider = AlpacaDataProvider(api_key="key_id", options={"secret_key": "shh"})
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _json_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


def test_capabilities_include_crypto() -> None:
    provider = AlpacaDataProvider(api_key="key_id", options={"secret_key": "shh"})
    caps = provider.capabilities()
    assert DataCapability.CRYPTO in caps
    # existing capabilities preserved (no equity regression)
    assert {DataCapability.QUOTES, DataCapability.BARS, DataCapability.OPTIONS,
            DataCapability.NEWS} <= caps


async def test_crypto_quote_parses_v1beta3_multi_symbol_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json_response(
            {"quotes": {"BTC/USD": {
                "bp": 61234.567890123, "ap": 61240.111,
                "bs": 0.5, "as": 1.25, "t": "2026-07-17T12:00:00Z",
            }}}
        )

    provider = _provider(handler)
    quote = await provider.quote("BTC/USD")

    # routed to the crypto v1beta3 latest-quotes endpoint with symbols=BTC/USD
    assert seen[0].url.path == "/v1beta3/crypto/us/latest/quotes"
    assert seen[0].url.params.get("symbols") == "BTC/USD"
    # never routed to the stocks endpoint
    assert "/v2/stocks/" not in str(seen[0].url)

    assert quote.symbol == "BTC/USD"
    assert quote.bid == Decimal("61234.567890123")
    assert quote.ask == Decimal("61240.111")
    assert isinstance(quote.bid, Decimal) and isinstance(quote.ask, Decimal)
    assert quote.source == "alpaca"


async def test_crypto_quote_missing_symbol_raises_provider_error() -> None:
    # keyed by symbol: an empty/other-keyed quotes block means no quote for us
    provider = _provider(lambda _req: _json_response({"quotes": {}}))
    with pytest.raises(ProviderError):
        await provider.quote("BTC/USD")


async def test_crypto_quote_unsupported_pair_rejected() -> None:
    # normalize_crypto_symbol guards before any HTTP call; a stablecoin quote 422s
    provider = _provider(lambda _req: _json_response({"quotes": {}}))
    with pytest.raises(UnsupportedSymbolError):
        await provider.quote("BTC/USDT")


async def test_crypto_bars_parses_v1beta3_multi_symbol_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json_response(
            # sort=desc: the API returns newest-first (the provider reverses)
            {"bars": {"BTC/USD": [
                {"o": 60800.75, "h": 62000.0, "l": 60500.0, "c": 61900.0,
                 "v": 9876.54321, "t": "2026-07-16T00:00:00Z"},
                {"o": 60000.5, "h": 61000.25, "l": 59500.1, "c": 60800.75,
                 "v": 12345.6789, "t": "2026-07-15T00:00:00Z"},
            ]}}
        )

    provider = _provider(handler)
    bars = await provider.bars("BTC/USD", timeframe="1d", limit=100)

    assert seen[0].url.path == "/v1beta3/crypto/us/bars"
    assert seen[0].url.params.get("symbols") == "BTC/USD"
    assert seen[0].url.params.get("timeframe") == "1Day"
    assert "/v2/stocks/" not in str(seen[0].url)

    assert len(bars) == 2
    # sorted chronological (asc) even though requested newest-first
    assert bars[0].start < bars[1].start
    assert bars[0].symbol == "BTC/USD"
    assert bars[0].open == Decimal("60000.5")
    assert bars[0].close == Decimal("60800.75")
    assert isinstance(bars[0].open, Decimal)


async def test_crypto_bars_empty_raises_provider_error() -> None:
    provider = _provider(lambda _req: _json_response({"bars": {}}))
    with pytest.raises(ProviderError):
        await provider.bars("BTC/USD", timeframe="1d", limit=100)


async def test_equity_quote_path_unchanged() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json_response(
            {"quote": {"bp": 190.12, "ap": 190.15, "bs": 3, "as": 5,
                       "t": "2026-07-17T12:00:00Z"}}
        )

    provider = _provider(handler)
    quote = await provider.quote("AAPL")

    # still the per-symbol stocks endpoint; crypto branch not taken for equities
    assert seen[0].url.path == "/v2/stocks/AAPL/quotes/latest"
    assert "/v1beta3/" not in str(seen[0].url)
    assert quote.symbol == "AAPL"
    assert quote.bid == Decimal("190.12")
    assert quote.bid_size == 3
