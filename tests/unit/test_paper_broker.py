"""Paper broker simulation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from poseidon.brokers.plugins.paper import PaperBroker
from poseidon.core.enums import BrokerCapability, OrderSide, OrderStatus, OrderType
from poseidon.core.errors import BrokerError
from poseidon.core.models import Order

from ..conftest import make_quote


def broker_with_price(price: str, tmp_path=None) -> PaperBroker:
    async def quote_fn(symbol: str):
        return make_quote(symbol, price)

    options: dict[str, object] = {"quote_fn": quote_fn, "starting_cash": "10000"}
    if tmp_path is not None:
        options["state_file"] = str(tmp_path / "paper.json")
    return PaperBroker(credentials={}, options=options)


def buy(qty: str, limit: str | None = "100.05") -> Order:
    return Order(symbol="AAPL", side=OrderSide.BUY,
                 order_type=OrderType.LIMIT if limit else OrderType.MARKET,
                 quantity=Decimal(qty),
                 limit_price=Decimal(limit) if limit else None)


async def test_market_buy_fills_at_ask() -> None:
    broker = broker_with_price("100.00")
    await broker.connect()
    order = await broker.submit_order(buy("10", None))
    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price == Decimal("100.05")  # ask side
    positions = await broker.positions()
    assert positions[0].quantity == Decimal("10")


async def test_nonmarketable_limit_rests() -> None:
    broker = broker_with_price("100.00")
    await broker.connect()
    order = await broker.submit_order(buy("10", "95.00"))
    assert order.status is OrderStatus.ACCEPTED
    assert len(await broker.open_orders()) == 1


async def test_insufficient_cash_rejected() -> None:
    broker = broker_with_price("100.00")
    await broker.connect()
    order = await broker.submit_order(buy("500"))  # 50k > 10k cash
    assert order.status is OrderStatus.REJECTED_BROKER
    assert "insufficient" in (order.status_reason or "")


async def test_sell_updates_cash_and_pnl() -> None:
    broker = broker_with_price("100.00")
    await broker.connect()
    await broker.submit_order(buy("10"))
    sell = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET,
                 quantity=Decimal("10"))
    result = await broker.submit_order(sell)
    assert result.status is OrderStatus.FILLED
    assert await broker.positions() == []
    account = await broker.account()
    assert account.day_pnl is not None


async def test_oversell_rejected() -> None:
    broker = broker_with_price("100.00")
    await broker.connect()
    sell = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET,
                 quantity=Decimal("5"))
    result = await broker.submit_order(sell)
    assert result.status is OrderStatus.REJECTED_BROKER


async def test_state_persists_across_restart(tmp_path) -> None:
    broker = broker_with_price("100.00", tmp_path)
    await broker.connect()
    await broker.submit_order(buy("10"))
    await broker.disconnect()

    revived = broker_with_price("100.00", tmp_path)
    await revived.connect()
    positions = await revived.positions()
    assert positions and positions[0].quantity == Decimal("10")
    lots = await revived.tax_lots("AAPL")
    assert lots and lots[0].quantity == Decimal("10")


async def test_no_quote_source_refuses_to_trade() -> None:
    broker = PaperBroker(credentials={}, options={"starting_cash": "1000"})
    await broker.connect()
    with pytest.raises(BrokerError, match="refusing to invent prices"):
        await broker.submit_order(buy("1", None))


def crypto_broker_with_price(price: str) -> PaperBroker:
    async def quote_fn(symbol: str):
        # Wide-enough cash for a fractional BTC buy; preserve full precision.
        return make_quote(symbol, price, spread="1.00")

    options: dict[str, object] = {"quote_fn": quote_fn, "starting_cash": "100000"}
    return PaperBroker(credentials={}, options=options)


def test_capabilities_include_crypto() -> None:
    broker = PaperBroker(credentials={}, options={})
    assert BrokerCapability.CRYPTO in broker.capabilities()


async def test_crypto_fractional_buy_fills_from_crypto_quote() -> None:
    # Crypto price carries many decimals; the fill must stay exact Decimal.
    broker = crypto_broker_with_price("60123.45678901")
    await broker.connect()
    order = Order(symbol="BTC/USD", side=OrderSide.BUY, order_type=OrderType.MARKET,
                  quantity=Decimal("0.05"))
    filled = await broker.submit_order(order)
    assert filled.status is OrderStatus.FILLED
    # Market buy fills at the ask (mid + half of the 1.00 spread).
    assert filled.avg_fill_price == Decimal("60123.95678901")
    assert isinstance(filled.avg_fill_price, Decimal)

    positions = await broker.positions()
    assert len(positions) == 1
    assert positions[0].symbol == "BTC/USD"
    assert positions[0].quantity == Decimal("0.05")
    assert isinstance(positions[0].quantity, Decimal)

    fills = await broker.recent_fills()
    assert fills and fills[0].price == Decimal("60123.95678901")
    assert isinstance(fills[0].price, Decimal)
