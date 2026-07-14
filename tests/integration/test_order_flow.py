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

from poseidon.brokers.plugins.paper import PaperBroker
from poseidon.core.clock import FreshnessPolicy, MarketClock
from poseidon.core.config import RiskConfig
from poseidon.core.enums import (
    DecisionAction,
    MarketSession,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from poseidon.core.events import EventBus
from poseidon.core.models import Decision, ExitPlan, Order, ProposedTrade, TradeRationale
from poseidon.data.router import DataRouter
from poseidon.execution.approvals import ApprovalQueue
from poseidon.execution.manager import OrderManager
from poseidon.portfolio.state import PortfolioState
from poseidon.portfolio.sync import PortfolioSyncService
from poseidon.risk.engine import RiskEngine
from poseidon.security.audit import AuditLog
from poseidon.storage.db import Database

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
           "audit": audit, "sync": sync, "risk": risk, "bus": bus, "portfolio": portfolio}
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


class TestManualTrading:
    """Operator-entered orders: same risk gate, no approval queue."""

    def _manual(self, qty: str = "10", limit: str = "100.05") -> Order:
        return Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                     quantity=Decimal(qty), limit_price=Decimal(limit), strategy="manual")

    async def test_manual_order_executes_in_autonomous(self, stack) -> None:
        order = await stack["manager"].submit_manual(self._manual())
        assert order.status is OrderStatus.FILLED
        assert order.strategy == "manual"
        assert order.arrival_price is not None  # TCA benchmark captured

    async def test_manual_order_skips_approval_queue_in_approval_mode(self, stack) -> None:
        stack["manager"].set_mode(TradingMode.APPROVAL)
        order = await stack["manager"].submit_manual(self._manual())
        assert order.status is OrderStatus.FILLED  # the human IS the approver
        assert stack["approvals"].pending() == []

    async def test_manual_order_refused_in_research(self, stack) -> None:
        stack["manager"].set_mode(TradingMode.RESEARCH)
        order = await stack["manager"].submit_manual(self._manual())
        assert order.status is OrderStatus.REJECTED_HUMAN
        assert "research mode" in (order.status_reason or "")

    async def test_manual_order_still_passes_risk(self, stack) -> None:
        # Fat-finger: notional above max_order_notional gets rejected.
        order = await stack["manager"].submit_manual(self._manual(qty="5000"))
        assert order.status is OrderStatus.REJECTED_RISK
        assert order.status_reason


async def test_f018_ambiguous_submit_reserves_exposure(tmp_path) -> None:
    # An ambiguous submit failure leaves the order possibly-live at the broker,
    # so it must reserve in-flight exposure (promote to _pending) or the NEXT
    # opening trade validates as if it does not exist and stacks past the caps.
    from poseidon.core.config import RiskConfig
    from poseidon.core.errors import BrokerError
    from poseidon.core.models import AccountSnapshot, Position

    bus = EventBus()
    router = DataRouter([(FakeProvider(name="feed", price="100"), 10)], FreshnessPolicy())
    broker = PaperBroker(credentials={}, options={
        "starting_cash": "100000", "state_file": str(tmp_path / "paper.json")})
    broker.set_quote_fn(lambda s: router.quote(s, allow_delayed=True))
    await broker.connect()
    db = Database(tmp_path / "t.db")
    await db.open()
    now = datetime.now(UTC)
    portfolio = PortfolioState()
    portfolio.account = AccountSnapshot(
        broker="paper", account_id="t", equity=Decimal("100000"),
        cash=Decimal("100000"), buying_power=Decimal("500000"), as_of=now)
    # $150k held -> gross 150k, so a 2.0x/$200k cap leaves exactly $50k headroom.
    portfolio.positions = [Position(symbol="HELD", quantity=Decimal("1500"),
                                    avg_entry_price=Decimal("100"), as_of=now)]
    portfolio.synced_at = now
    config = RiskConfig(max_leverage=2.0, max_portfolio_exposure_pct=2.0,
                        max_position_pct=1.0, max_order_notional=Decimal("100000"),
                        news_blackout_minutes_before_econ=0)
    risk = RiskEngine(config, portfolio, router, MarketClock(), bus)
    manager = OrderManager(broker, risk, ApprovalQueue(bus), db, AuditLog(db), bus,
                           mode=TradingMode.AUTONOMOUS)
    try:
        with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
            # A ($40k) validates (150k+40k=190k<200k), then submit raises ambiguous.
            a = Order(symbol="AAA", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                      quantity=Decimal("400"), limit_price=Decimal("100"))
            with patch.object(broker, "submit_order", side_effect=BrokerError(
                    "paper", "post-send timeout", retryable=False, ambiguous=True)):
                a = await manager.submit_manual(a)
            assert a.status is OrderStatus.ERROR
            # F018-specific: a possibly-live ambiguous order is promoted to the
            # DURABLE _pending reservation (released only when a sync proves it
            # gone), not left in the 15-min-blind-pruned _validated_notional stash.
            assert a.id in risk._pending
            assert a.id not in risk._validated_notional

            # B ($40k): post-fix, A's ambiguous reservation counts, so
            # 150k+40k+40k=230k > 200k and B is rejected. Pre-fix A reserved
            # nothing, so B passes and the two stack past the 2.0x cap.
            b = Order(symbol="BBB", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                      quantity=Decimal("400"), limit_price=Decimal("100"))
            b = await manager.submit_manual(b)
            assert b.status is OrderStatus.REJECTED_RISK, f"expected reject, got {b.status}"
    finally:
        await db.close()
        await bus.close()


async def test_f022_concurrent_market_exit_blocks_oversell(stack) -> None:
    # Two exits race on a 100-share long. A guardian MARKET SELL 100 fills
    # (position -> 0) but no re-sync happens, so a racing SELL 40 still validates
    # against the stale synced held=100. The submit-time backstop reads LIVE
    # positions() (=0) and rejects it at the RISK layer before the broker —
    # closing the between-syncs oversell window (F022).
    m = stack["manager"]
    buy = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=Decimal("100"))
    await m.submit_manual(buy)
    await stack["sync"].sync_once()
    assert stack["portfolio"].position_for("AAPL").quantity == Decimal("100")

    # Guardian's own MARKET exit fills the whole position (paper position -> 0).
    exit1 = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET,
                  quantity=Decimal("100"), strategy="guardian")
    await m.submit_manual(exit1)
    # NO re-sync: the synced snapshot still shows held=100, open_orders=[].
    assert stack["portfolio"].position_for("AAPL").quantity == Decimal("100")

    # Racing second exit: stale validation would pass (held 100), but the
    # live-state backstop (positions()=0) rejects it. Pre-fix it reaches the
    # broker and comes back REJECTED_BROKER (paper: insufficient position).
    exit2 = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET,
                  quantity=Decimal("40"))
    exit2 = await m.submit_manual(exit2)
    assert exit2.status is OrderStatus.REJECTED_RISK, f"got {exit2.status}"
    assert "reduce_only" in (exit2.status_reason or "")


async def test_f022_lone_full_exit_is_not_trapped(stack) -> None:
    # The backstop must NEVER block a legitimate lone exit of the true position:
    # its own order is not yet at the broker, so live held=100 and working=0 give
    # available=100 >= the SELL 100 — it fills. (Guards the never-trap-an-exit
    # half of the reduce-only invariant against a future over-eager backstop.)
    m = stack["manager"]
    await m.submit_manual(Order(symbol="AAPL", side=OrderSide.BUY,
                                order_type=OrderType.MARKET, quantity=Decimal("100")))
    await stack["sync"].sync_once()
    exit_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET,
                       quantity=Decimal("100"))
    exit_order = await m.submit_manual(exit_order)
    assert exit_order.status is OrderStatus.FILLED, f"lone exit trapped: {exit_order.status}"
