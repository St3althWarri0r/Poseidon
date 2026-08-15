"""A SELL clears same-symbol resting BUYs before it reaches the broker.

Live failure this pins: a stale GTC buy resting at the broker made every
LINK/USD exit die on alpaca's self-trade block (HTTP 403 "potential wash
trade detected"), leaving the position un-closable. The platform never opens
shorts, so an incoming SELL is always position-closing and a same-symbol
resting BUY is contrary intent: cancel it first. BUYs never clear anything —
a guardian's resting protective take-profit SELL must survive a new entry.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from poseidon.brokers.plugins.paper import PaperBroker
from poseidon.core.clock import FreshnessPolicy, MarketClock
from poseidon.core.config import RiskConfig
from poseidon.core.enums import (
    MarketSession,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    TradingMode,
)
from poseidon.core.events import EventBus
from poseidon.core.models import Order
from poseidon.data.router import DataRouter
from poseidon.execution.approvals import ApprovalQueue
from poseidon.execution.manager import OrderManager
from poseidon.portfolio.state import PortfolioState
from poseidon.portfolio.sync import PortfolioSyncService
from poseidon.risk.engine import RiskEngine
from poseidon.security.audit import AuditLog
from poseidon.storage.db import Database

from ..conftest import FakeProvider


def _order(symbol: str, side: OrderSide, qty: str = "5", *,
           status: OrderStatus = OrderStatus.SUBMITTED, broker: str = "paper") -> Order:
    return Order(symbol=symbol, side=side, order_type=OrderType.LIMIT,
                 quantity=Decimal(qty), limit_price=Decimal("100"),
                 time_in_force=TimeInForce.GTC, status=status, broker=broker,
                 strategy="test", created_at=datetime.now(UTC))


@pytest.fixture
async def stack(tmp_path):
    bus = EventBus()
    router = DataRouter([(FakeProvider(name="feed"), 10)], FreshnessPolicy())
    broker = PaperBroker(credentials={}, options={
        "starting_cash": "100000", "state_file": str(tmp_path / "paper.json"),
    })
    broker.set_quote_fn(lambda s: router.quote(s, allow_delayed=True))
    await broker.connect()
    db = Database(tmp_path / "test.db")
    await db.open()
    audit = AuditLog(db)
    portfolio = PortfolioState()
    clock = MarketClock()
    sync = PortfolioSyncService(broker, portfolio, bus, db, clock)
    await sync.sync_once()
    risk = RiskEngine(RiskConfig(news_blackout_minutes_before_econ=0),
                      portfolio, router, clock, bus)
    manager = OrderManager(broker, risk, ApprovalQueue(bus), db, audit, bus,
                           mode=TradingMode.AUTONOMOUS)
    session_patch = patch.object(MarketClock, "session",
                                 return_value=MarketSession.REGULAR)
    session_patch.start()
    yield {"manager": manager, "broker": broker, "db": db, "audit": audit, "sync": sync}
    session_patch.stop()
    await bus.close()


async def _persist_resting(db: Database, order: Order) -> None:
    await db.execute(
        "INSERT INTO orders (id, client_order_id, broker, payload, status, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order.id, order.client_order_id, order.broker,
         json.dumps(order.model_dump(mode="json"), default=str),
         order.status.value, order.created_at.isoformat(), order.created_at.isoformat()),
    )


async def _audit_actions(db: Database) -> list[str]:
    rows = await db.fetch_all("SELECT action FROM audit ORDER BY seq")
    return [r[0] for r in rows]


async def test_sell_cancels_same_symbol_resting_buy(stack) -> None:
    resting_buy = _order("AAPL", OrderSide.BUY)
    canceled: list[str] = []

    async def fake_cancel(order: Order) -> Order:
        canceled.append(order.id)
        order.status = OrderStatus.CANCELED
        return order

    await _persist_resting(stack["db"], resting_buy)
    stack["broker"].cancel_order = fake_cancel  # type: ignore[method-assign]
    sell = _order("AAPL", OrderSide.SELL, broker="")
    sell.status = OrderStatus.APPROVED
    await stack["manager"]._clear_opposing_orders(sell, stack["broker"])
    assert canceled == [resting_buy.id]
    actions = await _audit_actions(stack["db"])
    assert "exit.opposing_order_canceled" in actions
    # The canceled state was persisted over the resting row.
    row = await stack["db"].fetch_all(
        "SELECT status FROM orders WHERE id = ?", (resting_buy.id,))
    assert row[0][0] == OrderStatus.CANCELED.value


async def test_buy_never_cancels_resting_sell(stack) -> None:
    protective_sell = _order("AAPL", OrderSide.SELL)  # e.g. guardian take-profit
    canceled: list[str] = []

    async def fake_cancel(order: Order) -> Order:  # pragma: no cover - must not run
        canceled.append(order.id)
        return order

    await _persist_resting(stack["db"], protective_sell)
    stack["broker"].cancel_order = fake_cancel  # type: ignore[method-assign]
    buy = _order("AAPL", OrderSide.BUY, broker="")
    await stack["manager"]._clear_opposing_orders(buy, stack["broker"])
    assert canceled == []


async def test_other_symbol_and_cross_broker_buys_are_untouched(stack) -> None:
    other_symbol = _order("MSFT", OrderSide.BUY)
    cross_broker = _order("AAPL", OrderSide.BUY, broker="alpaca")
    canceled: list[str] = []

    async def fake_cancel(order: Order) -> Order:
        canceled.append(order.id)
        order.status = OrderStatus.CANCELED
        return order

    for o in (other_symbol, cross_broker):
        await _persist_resting(stack["db"], o)
    stack["broker"].cancel_order = fake_cancel  # type: ignore[method-assign]
    sell = _order("AAPL", OrderSide.SELL, broker="")
    await stack["manager"]._clear_opposing_orders(sell, stack["broker"])
    assert canceled == []


async def test_cancel_failure_is_audited_and_does_not_block(stack) -> None:
    resting_buy = _order("AAPL", OrderSide.BUY)

    async def broken_cancel(order: Order) -> Order:
        raise RuntimeError("broker wedged")

    await _persist_resting(stack["db"], resting_buy)
    stack["broker"].cancel_order = broken_cancel  # type: ignore[method-assign]
    sell = _order("AAPL", OrderSide.SELL, broker="")
    # Must not raise: the exit proceeds even when the cancel fails.
    await stack["manager"]._clear_opposing_orders(sell, stack["broker"])
    actions = await _audit_actions(stack["db"])
    assert "exit.opposing_cancel_failed" in actions


async def test_end_to_end_sell_clears_buy_then_submits(stack) -> None:
    """Through the real _submit path: buy a position, park a resting buy, then a
    full exit sell both cancels the resting buy and fills the exit."""
    manager, broker, db = stack["manager"], stack["broker"], stack["db"]
    # Open a position through the normal path so reduce-only sees it.
    from .test_p1_manager import make_decision  # same-harness factory
    buys = await manager.execute_decision(make_decision("10"))
    assert buys[0].status is OrderStatus.FILLED
    await stack["sync"].sync_once()  # reduce-only must see the live position
    await stack["db"].execute("DELETE FROM orders")  # forget the filled entry row
    resting_buy = _order("AAPL", OrderSide.BUY)
    await _persist_resting(db, resting_buy)
    real_cancel = broker.cancel_order
    canceled: list[str] = []

    async def spy_cancel(order: Order) -> Order:
        canceled.append(order.id)
        return await real_cancel(order)

    broker.cancel_order = spy_cancel  # type: ignore[method-assign]
    from .test_p1_manager import make_exit_decision
    exits = await manager.execute_decision(make_exit_decision("10"))
    assert canceled == [resting_buy.id]
    assert exits[0].status is OrderStatus.FILLED
