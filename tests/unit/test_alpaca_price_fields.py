"""Alpaca price fields must match the order TYPE, not merely be present.

The body attached `limit_price` and `stop_price` whenever the order carried
them, independent of `order_type`. A limit order that also carried a stop price
therefore sent both, and Alpaca refused it outright. Observed live, on the first
order the AI managed to place after the sizing fix:

    ADA/USD buy 1,100,654.8896 -> rejected_broker
    [alpaca] HTTP 422 POST /v2/orders:
      {"code":40010001,"message":"limit orders require no stop price"}

The order was well-formed by Poseidon's own rules and died at the broker for a
field it should never have sent. Alpaca's contract:

    market        neither price
    limit         limit_price only
    stop          stop_price only
    stop_limit    both
    trailing_stop neither (trail_price / trail_percent instead)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from poseidon.brokers.plugins.alpaca import AlpacaBroker
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.models import Order


def _body(order_type: OrderType, *, limit: str | None, stop: str | None) -> dict:
    order = Order(
        symbol="ADA/USD", side=OrderSide.BUY, quantity=Decimal("100"),
        asset_class=AssetClass.CRYPTO, order_type=order_type,
        limit_price=Decimal(limit) if limit else None,
        stop_price=Decimal(stop) if stop else None,
        strategy="momentum",
    )
    return AlpacaBroker._order_body(order)  # noqa: SLF001


def test_a_limit_order_never_sends_a_stop_price() -> None:
    """The exact live rejection: both prices present, type=limit."""
    body = _body(OrderType.LIMIT, limit="0.55", stop="0.50")
    assert body["type"] == "limit"
    assert body["limit_price"] == "0.55"
    assert "stop_price" not in body, (
        "Alpaca rejects a limit order carrying a stop price: "
        '{"code":40010001,"message":"limit orders require no stop price"}'
    )


def test_a_stop_order_sends_only_the_stop_price() -> None:
    body = _body(OrderType.STOP, limit="0.55", stop="0.50")
    assert body["stop_price"] == "0.50"
    assert "limit_price" not in body


def test_a_stop_limit_order_sends_both() -> None:
    body = _body(OrderType.STOP_LIMIT, limit="0.55", stop="0.50")
    assert body["limit_price"] == "0.55"
    assert body["stop_price"] == "0.50"


@pytest.mark.parametrize("kind", [OrderType.MARKET, OrderType.TRAILING_STOP])
def test_market_and_trailing_stop_send_neither(kind: OrderType) -> None:
    body = _body(kind, limit="0.55", stop="0.50")
    assert "limit_price" not in body
    assert "stop_price" not in body


def test_a_limit_order_without_a_stop_is_unchanged() -> None:
    """The common path must be byte-identical to before."""
    body = _body(OrderType.LIMIT, limit="0.55", stop=None)
    assert body["limit_price"] == "0.55"
    assert "stop_price" not in body


def test_the_rest_of_the_body_still_carries_the_essentials() -> None:
    body = _body(OrderType.LIMIT, limit="0.55", stop="0.50")
    assert body["symbol"] == "ADA/USD"
    assert body["side"] == "buy"
    assert body["qty"] == "100"
    assert body["client_order_id"]
    assert body["time_in_force"] == "gtc"      # crypto TIF coercion preserved
    assert "extended_hours" not in body        # crypto omits it
