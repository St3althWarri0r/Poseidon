"""Integration: decision -> risk engine -> order manager -> paper broker.

Exercises the real components together (only market data is faked), across
all three operating modes, including duplicate prevention and risk
rejection paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from aegis_trader.brokers.plugins.paper import PaperBroker
from aegis_trader.core.clock import FreshnessPolicy, MarketClock
from aegis_trader.core.config import RiskConfig
from aegis_trader.core.enums import (
    DecisionAction,
    MarketSession,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from aegis_trader.core.events import EventBus
from aegis_trader.core.models import Decision, ExitPlan, ProposedTrade, TradeRationale
from aegis_trader.data.router import DataRouter
from aegis_trader.execution.approvals import ApprovalQueue
from aegis_trader.execution.manager import OrderManager
from aegis_trader.portfolio.state import PortfolioState
from aegis_trader.portfolio.sync import PortfolioSyncService
from aegis_trader.risk.engine import RiskEngine
from aegis_trader.security.audit import AuditLog
from aegis_trader.storage.db import Database

from ..conftest import FakeProvider


def make_decision(qty: str = "10", limit: str = "100.05") -> Decision:
    return Decision(
        action=DecisionAction.BUY,
        trades=[ProposedTrade(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                              quantity=Decimal(qty), limit_price=Decimal(limit),
                              strategy="momentum")],
        rationale=TradeRationale(
            thesis="test", timing="now", expected_edge="e", risk="r", reward="w",
            confidence=0.8, portfolio_impact="small", exit_plan=ExitPlan(),
            max_expected_loss="$100",
        ),
        cycle_id="itest", created_at=datetime.now(UTC),
    )


@pytest.fixture
async def stack(tmp_path):
    """Real components wired together over a fake data feed."""
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
    approvals = ApprovalQueue(bus)
    manager = OrderManager(broker, risk, approvals, db, audit, bus, mode=TradingMode.AUTONOMOUS)
    # Force market-open for deterministic tests.
    session_patch = patch.object(MarketClock, "session", return_value=MarketSession.REGULAR)
    session_patch.start()
    yield {"manager": manager, "approvals": approvals, "broker": broker, "db": db,
           "audit": audit, "sync": sync, "risk": risk, "bus": bus}
    session_patch.stop()
    await bus.close()
    await db.close()


async def test_autonomous_buy_executes(stack) -> None:
    orders = await stack["manager"].execute_decision(make_decision())
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.FILLED
    positions = await stack["broker"].positions()
    assert positions[0].symbol == "AAPL"
    # Audit trail recorded submission and fill-side entries.
    ok, _ = await stack["audit"].verify_chain()
    assert ok


async def test_research_mode_never_submits(stack) -> None:
    stack["manager"].set_mode(TradingMode.RESEARCH)
    orders = await stack["manager"].execute_decision(make_decision())
    assert orders[0].status is OrderStatus.REJECTED_HUMAN
    assert "research mode" in (orders[0].status_reason or "")
    assert await stack["broker"].open_orders() == []


async def test_approval_mode_waits_for_human(stack) -> None:
    import asyncio

    stack["manager"].set_mode(TradingMode.APPROVAL)

    async def approve_soon() -> None:
        for _ in range(100):
            pending = stack["approvals"].pending()
            if pending:
                stack["approvals"].resolve(pending[0].order.id, approved=True)
                return
            await asyncio.sleep(0.02)
        raise AssertionError("approval never appeared")

    approver = asyncio.create_task(approve_soon())
    orders = await stack["manager"].execute_decision(make_decision())
    await approver
    assert orders[0].status is OrderStatus.FILLED


async def test_approval_mode_rejection(stack) -> None:
    import asyncio

    stack["manager"].set_mode(TradingMode.APPROVAL)

    async def reject_soon() -> None:
        for _ in range(100):
            pending = stack["approvals"].pending()
            if pending:
                stack["approvals"].resolve(pending[0].order.id, approved=False)
                return
            await asyncio.sleep(0.02)

    rejecter = asyncio.create_task(reject_soon())
    orders = await stack["manager"].execute_decision(make_decision())
    await rejecter
    assert orders[0].status is OrderStatus.REJECTED_HUMAN


async def test_risk_rejects_oversized_order(stack) -> None:
    # 10% position cap on 100k equity → 10k; 200 * 100.05 ≈ 20k breaches.
    orders = await stack["manager"].execute_decision(make_decision(qty="200"))
    assert orders[0].status is OrderStatus.REJECTED_RISK
    assert "max_position_size" in (orders[0].status_reason or "")


async def test_duplicate_identical_open_order_blocked(stack) -> None:
    # First: a resting (non-marketable) limit order stays open at the broker.
    resting = make_decision(qty="10", limit="90.00")
    # Bypass slippage-band rejection by pricing within the band but below bid.
    resting.trades[0].limit_price = Decimal("99.20")
    stack["risk"]._config.slippage_limit_pct = 0.02  # widen band for the test
    first = await stack["manager"].execute_decision(resting)
    assert first[0].status is OrderStatus.ACCEPTED
    second = await stack["manager"].execute_decision(resting)
    assert second[0].status in (OrderStatus.ERROR, OrderStatus.REJECTED_BROKER, OrderStatus.REJECTED_RISK)


async def test_sync_baselines_and_drawdown(stack) -> None:
    portfolio = stack["sync"]._state
    assert portfolio.account is not None
    assert portfolio.day_start_equity is not None
    assert portfolio.drawdown_pct() >= 0.0
