"""Coinbase Exchange public data provider (Task 1). No network.

Drives ``CoinbaseDataProvider.quote()``/``bars()`` over an ``httpx.MockTransport``
so the real ``_get``/``_get_json``/``_decode`` + JSON parse runs, exactly like the
Alpaca crypto tests. Coinbase's public REST API (``api.exchange.coinbase.com``)
needs no key: the ticker endpoint is per-product and returns string prices plus a
last-trade ``time``; candles are ``[time, low, high, open, close, volume]`` rows,
newest-first, with ``time`` in unix seconds. These tests pin the Decimal parse,
the honest ``as_of`` (from the trade time, never ``now()``), the ``BTC/USD`` ->
``BTC-USD`` conversion, and the crypto-only contract (never equity data).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from poseidon.core.errors import DataError, ProviderError, UnsupportedSymbolError
from poseidon.data.base import DataCapability
from poseidon.data.providers import BUILTIN_PROVIDERS
from poseidon.data.providers.coinbase_data import CoinbaseDataProvider


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> CoinbaseDataProvider:
    provider = CoinbaseDataProvider(api_key="")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _json_response(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


def test_registered_in_builtin_providers() -> None:
    assert BUILTIN_PROVIDERS.get("coinbase") is CoinbaseDataProvider


def test_capabilities_are_crypto_only() -> None:
    caps = CoinbaseDataProvider(api_key="").capabilities()
    assert DataCapability.CRYPTO in caps
    assert {DataCapability.QUOTES, DataCapability.BARS} <= caps
    # crypto-only: never advertise equity-flavoured capabilities
    assert DataCapability.OPTIONS not in caps
    assert DataCapability.NEWS not in caps


def test_no_api_key_required() -> None:
    # Coinbase's public endpoints take no key; construction must not raise.
    provider = CoinbaseDataProvider(api_key="")
    assert provider.name == "coinbase"


async def test_quote_parses_ticker_decimal_and_converts_symbol() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json_response(
            {
                "trade_id": 123,
                "price": "61234.567890123",
                "size": "0.0123",
                "bid": "61234.55",
                "ask": "61240.111",
                "volume": "12345.6789",
                "time": "2026-07-17T12:00:00.123456Z",
            }
        )

    provider = _provider(handler)
    quote = await provider.quote("BTC/USD")

    # BTC/USD -> BTC-USD in the per-product ticker path
    assert seen[0].url.path == "/products/BTC-USD/ticker"

    assert quote.symbol == "BTC/USD"
    assert quote.bid == Decimal("61234.55")
    assert quote.ask == Decimal("61240.111")
    assert quote.last == Decimal("61234.567890123")
    assert isinstance(quote.bid, Decimal)
    assert isinstance(quote.ask, Decimal)
    assert isinstance(quote.last, Decimal)
    assert quote.source == "coinbase"


async def test_quote_as_of_is_trade_time_not_now() -> None:
    ticker_time = "2026-07-17T12:00:00.123456Z"
    provider = _provider(
        lambda _req: _json_response(
            {"price": "61000", "bid": "60999", "ask": "61001", "time": ticker_time}
        )
    )
    quote = await provider.quote("BTC/USD")

    expected = datetime(2026, 7, 17, 12, 0, 0, 123456, tzinfo=UTC)
    assert quote.as_of == expected
    # honest freshness: never stamp receipt time
    assert quote.as_of != provider._now()
    assert abs((provider._now() - quote.as_of).total_seconds()) > 60


async def test_quote_missing_time_raises_rather_than_fabricating() -> None:
    provider = _provider(
        lambda _req: _json_response({"price": "61000", "bid": "60999", "ask": "61001"})
    )
    with pytest.raises(ProviderError):
        await provider.quote("BTC/USD")


async def test_quote_non_crypto_symbol_raises_and_makes_no_call() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return _json_response({})

    provider = _provider(handler)
    with pytest.raises(DataError):
        await provider.quote("AAPL")
    # never reached the network — crypto-only guard fires before any HTTP
    assert calls == []


async def test_quote_unsupported_pair_rejected() -> None:
    provider = _provider(lambda _req: _json_response({}))
    with pytest.raises(UnsupportedSymbolError):
        await provider.quote("BTC/USDT")


async def test_bars_parses_candles_decimal_and_chronological() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        # Coinbase candles: [time, low, high, open, close, volume], newest-first,
        # time in unix seconds.
        return _json_response(
            [
                [1_752_710_400, 60500.0, 62000.0, 60800.75, 61900.0, 9876.54321],
                [1_752_624_000, 59500.1, 61000.25, 60000.5, 60800.75, 12345.6789],
            ]
        )

    provider = _provider(handler)
    bars = await provider.bars("BTC/USD", timeframe="1d", limit=100)

    assert seen[0].url.path == "/products/BTC-USD/candles"
    assert seen[0].url.params.get("granularity") == "86400"

    assert len(bars) == 2
    # returned newest-first; provider reverses to chronological
    assert bars[0].start < bars[1].start
    assert bars[0].symbol == "BTC/USD"
    assert bars[0].open == Decimal("60000.5")
    assert bars[0].close == Decimal("60800.75")
    assert bars[0].high == Decimal("61000.25")
    assert bars[0].low == Decimal("59500.1")
    assert isinstance(bars[0].open, Decimal)
    assert bars[0].source == "coinbase"


async def test_bars_respects_limit() -> None:
    rows = [
        [1_752_710_400 + i * 86_400, 1.0, 2.0, 1.5, 1.8, 100.0] for i in range(10)
    ]
    provider = _provider(lambda _req: _json_response(rows))
    bars = await provider.bars("BTC/USD", timeframe="1d", limit=3)
    assert len(bars) == 3


async def test_bars_unsupported_timeframe_raises() -> None:
    # Coinbase has no weekly granularity.
    provider = _provider(lambda _req: _json_response([]))
    with pytest.raises(ProviderError):
        await provider.bars("BTC/USD", timeframe="1w", limit=10)


async def test_bars_empty_raises_provider_error() -> None:
    provider = _provider(lambda _req: _json_response([]))
    with pytest.raises(ProviderError):
        await provider.bars("BTC/USD", timeframe="1d", limit=10)


async def test_bars_non_crypto_symbol_raises_and_makes_no_call() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return _json_response([])

    provider = _provider(handler)
    with pytest.raises(DataError):
        await provider.bars("AAPL", timeframe="1d", limit=10)
    assert calls == []
