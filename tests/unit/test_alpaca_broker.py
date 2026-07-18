"""Alpaca ``submit_order`` request-body shaping.

Pins the crypto time-in-force / extended-hours fix. Alpaca rejects equity-style
order fields on crypto: the equity TIFs (``day``/``opg``/``cls``) return
HTTP 422 ``42210000`` "invalid crypto time_in_force", and ``extended_hours`` is
equity-only. So a crypto order must send a crypto-valid TIF and must NOT send
``extended_hours``; equities/options are unchanged.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from poseidon.brokers.plugins.alpaca import AlpacaBroker
from poseidon.core.enums import AssetClass, OrderSide, OrderType, TimeInForce
from poseidon.core.models import Order

_CREDS = {"key_id": "k", "secret_key": "s"}


def _broker_capturing(captured: dict[str, Any]) -> AlpacaBroker:
    """An ``AlpacaBroker`` whose HTTP client records the POSTed order body into
    ``captured["body"]`` and returns a minimal accepted-order response. No
    network: the transport is an in-process ``httpx.MockTransport``."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"id": "broker-order-1", "status": "accepted"},
            headers={"content-type": "application/json"},
        )

    broker = AlpacaBroker(credentials=_CREDS)
    broker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return broker


async def test_crypto_order_uses_gtc_and_omits_extended_hours() -> None:
    # BTC/USD market order carrying the equity default TIF (day). Alpaca rejects
    # `day` for crypto, so it must be remapped to a crypto-valid `gtc`, and the
    # equity-only `extended_hours` field must be absent from the body entirely.
    captured: dict[str, Any] = {}
    broker = _broker_capturing(captured)
    order = Order(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        time_in_force=TimeInForce.DAY,
    )
    await broker.submit_order(order)
    body = captured["body"]
    assert body["time_in_force"] == "gtc"
    assert "extended_hours" not in body
    assert body["symbol"] == "BTC/USD"


async def test_equity_order_keeps_tif_and_extended_hours() -> None:
    # Equity path is unchanged: the order's own TIF is sent verbatim and the
    # equity-only extended_hours flag is included.
    captured: dict[str, Any] = {}
    broker = _broker_capturing(captured)
    order = Order(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        time_in_force=TimeInForce.DAY,
        extended_hours=True,
    )
    await broker.submit_order(order)
    body = captured["body"]
    assert body["time_in_force"] == "day"
    assert body["extended_hours"] is True


async def test_crypto_order_preserves_already_valid_tif() -> None:
    # gtc/ioc/fok are valid crypto TIFs: only the non-crypto day/opg/cls are
    # remapped. A deliberate immediate-or-cancel must NOT be silently converted
    # to gtc (which would rest on the book). extended_hours is still omitted.
    captured: dict[str, Any] = {}
    broker = _broker_capturing(captured)
    order = Order(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.5"),
        time_in_force=TimeInForce.IOC,
    )
    await broker.submit_order(order)
    body = captured["body"]
    assert body["time_in_force"] == "ioc"
    assert "extended_hours" not in body
