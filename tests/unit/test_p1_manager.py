"""Regression pins for OrderManager audit/halt fixes F015/F016/F017 (commit 3a10e42).

Each test drives the real OrderManager + RiskEngine + PaperBroker + AuditLog + DB
over a FakeProvider feed (the integration ``stack`` harness) and asserts the
*specific* audit / publish / submit behaviour the fix added, so the fix cannot
silently regress on this real-money path.

One focused test per finding. The discriminating assertion ("the tooth") in each
is the exact effect the fix introduced; the surrounding status/reason checks were
already true on pre-fix code and are asserted only as sanity that the right branch
was reached (except F017, where status/reason also discriminate — see its note).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from poseidon.brokers.plugins.paper import PaperBroker
from poseidon.core.clock import FreshnessPolicy, MarketClock
from poseidon.core.config import RiskConfig
from poseidon.core.enums import (
    AssetClass,
    DecisionAction,
    MarketSession,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from poseidon.core.errors import DataError
from poseidon.core.events import EventBus, Topics
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
        cycle_id="p1test", created_at=datetime.now(UTC),
    )


def make_exit_decision(qty: str = "10") -> Decision:
    """A risk-reducing MARKET SELL — the shape the guardian dispatches through
    ``execute_decision`` when a stop/target fires. Even this must be blocked by a
    tripped breaker (the normal-order paths carry no halt token)."""
    return Decision(
        action=DecisionAction.SELL,
        trades=[ProposedTrade(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET,
                              quantity=Decimal(qty), strategy="guardian_exit")],
        rationale=TradeRationale(
            thesis="stop hit", timing="now", expected_edge="e", risk="r", reward="w",
            confidence=0.8, portfolio_impact="small", exit_plan=ExitPlan(),
            max_expected_loss="$100",
        ),
        cycle_id="p1exit", created_at=datetime.now(UTC),
    )


@pytest.fixture
async def stack(tmp_path):
    """Real components wired together over a fake data feed — a copy of the
    integration harness in tests/integration/test_order_flow.py so these unit
    pins never collide with (or depend on) their integration siblings."""
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


def _record_audit(audit: AuditLog) -> list[tuple[str, str, dict]]:
    """Wrap audit.append with a spy that records (actor, action, payload) and
    still delegates to the real hash-chained writer. Returns the capture list."""
    calls: list[tuple[str, str, dict]] = []
    real = audit.append

    async def spy(actor, action, payload=None):
        calls.append((actor, action, payload or {}))
        return await real(actor, action, payload)

    audit.append = spy
    return calls


def _record_publish(bus: EventBus) -> list[tuple[str, object]]:
    """Wrap bus.publish with a spy that records (topic, payload) and still
    delegates. Returns the capture list."""
    calls: list[tuple[str, object]] = []
    real = bus.publish

    async def spy(topic, payload=None):
        calls.append((topic, payload))
        return await real(topic, payload)

    bus.publish = spy
    return calls


# F015 — a validate_order DataError rejection must now enter the audit chain.
# Guards the defect: pre-fix the `except DataError` branch only persisted +
# published ORDER_REJECTED and never called audit.append, so a data-outage
# rejection left no tamper-evident record (inconsistent with the sibling
# RiskViolation branch). The sole discriminator is the audit.append below —
# status, reason and the publish were all already present pre-fix.
async def test_f015_data_error_rejection_is_audited(stack) -> None:
    audit_calls = _record_audit(stack["audit"])
    # Make the mandatory risk gate fail with DataError (live context unavailable).
    stack["risk"].validate_order = AsyncMock(side_effect=DataError("all providers down"))

    orders = await stack["manager"].execute_decision(make_decision())

    assert len(orders) == 1
    order = orders[0]
    # Sanity: the DataError branch was reached (already true on pre-fix code).
    assert order.status is OrderStatus.REJECTED_RISK
    assert "required live data unavailable" in (order.status_reason or "")
    # The tooth: the rejection is appended to the audit chain with cause
    # data_unavailable. Pre-fix this append did not exist -> this assertion fails.
    assert any(
        actor == "risk" and action == "order.rejected"
        and payload.get("cause") == "data_unavailable"
        for (actor, action, payload) in audit_calls
    ), f"expected a data_unavailable audit.append; got {audit_calls}"


# F016 — a post-approval re-validation failure must audit(stage=post_approval)
# AND publish ORDER_REJECTED. Guards the defect: pre-fix that `except` branch
# only persisted + returned, so a human-APPROVED order that failed the re-check
# vanished from both the audit chain and the dashboard (an approved order with
# no recorded outcome). Status/reason were already set pre-fix; the two teeth
# are the audit append with stage=post_approval and the ORDER_REJECTED publish.
async def test_f016_post_approval_rejection_is_audited_and_published(stack) -> None:
    stack["manager"].set_mode(TradingMode.APPROVAL)
    audit_calls = _record_audit(stack["audit"])
    published = _record_publish(stack["bus"])
    # Pass the pre-approval gate (1st call), fail the post-approval re-check (2nd).
    stack["risk"].validate_order = AsyncMock(side_effect=[None, DataError("quote went stale")])
    # The human approves — resolve the queue deterministically (no polling/sleep).
    stack["approvals"].wait = AsyncMock(return_value=True)

    orders = await stack["manager"].execute_decision(make_decision())

    assert len(orders) == 1
    order = orders[0]
    # Sanity: the post-approval rejection branch was reached (true pre-fix too).
    assert order.status is OrderStatus.REJECTED_RISK
    assert "post-approval re-check failed" in (order.status_reason or "")
    # Tooth 1: the rejection of the approved order now enters the audit chain.
    assert any(
        action == "order.rejected" and payload.get("stage") == "post_approval"
        for (_actor, action, payload) in audit_calls
    ), f"expected a post_approval audit.append; got {audit_calls}"
    # Tooth 2: it is also published (dashboard sees the approved->rejected reversal).
    assert any(topic == Topics.ORDER_REJECTED for (topic, _payload) in published), \
        f"expected an ORDER_REJECTED publish; got topics {[t for (t, _p) in published]}"


# F017 — if the circuit opens during the validate->submit window, _submit must
# reject BEFORE the broker call. Guards the defect: pre-fix _submit had no
# pre-submit circuit re-check, so an operator HALT / filesystem sentinel / error-
# rate trip that fires after validation still let a real order reach the broker.
# Here status/reason discriminate too: pre-fix the order flows to the paper
# broker and fills, so it is never REJECTED_RISK / "halted before submit".
async def test_f017_circuit_open_before_submit_blocks_broker(stack) -> None:
    manager = stack["manager"]
    broker = stack["broker"]
    # The breaker trips after validation would have passed (e.g. operator HALT).
    stack["risk"].circuit.force_open("operator HALT")

    submit_calls: list[Order] = []
    real_submit = broker.submit_order

    async def submit_spy(order):
        submit_calls.append(order)
        return await real_submit(order)

    broker.submit_order = submit_spy

    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                  quantity=Decimal("10"), limit_price=Decimal("100.05"), strategy="manual")
    result = await manager._submit(order)

    # Tooth: the broker was never asked to submit a halted order.
    assert submit_calls == [], "broker.submit_order was called despite an open circuit"
    # Tooth: the order is a halt rejection, not a fill (pre-fix it reaches + fills).
    assert result.status is OrderStatus.REJECTED_RISK
    assert "halted before submit" in (result.status_reason or "")


# --- Task 6: crypto asset-class tagging on the AI proposal path ---------------
# A ProposedTrade whose symbol is a slash-form crypto pair (BTC/USD) must be
# tagged CRYPTO even though the field defaults to EQUITY — no equity ticker
# contains '/', so the shape is unambiguous. This keeps the AI/manual/API paths
# consistent through the one shared `asset_class_for_symbol` helper.
def test_proposed_trade_tags_crypto_symbol() -> None:
    trade = ProposedTrade(symbol="BTC/USD", side=OrderSide.BUY,
                          order_type=OrderType.MARKET, quantity=Decimal("0.05"),
                          strategy="momentum")
    assert trade.asset_class is AssetClass.CRYPTO


# A plain equity ticker keeps the EQUITY default — no regression on the equity
# path the AI watchlist actually trades today.
def test_proposed_trade_leaves_equity_symbol_untagged() -> None:
    trade = ProposedTrade(symbol="AAPL", side=OrderSide.BUY,
                          order_type=OrderType.MARKET, quantity=Decimal("10"),
                          strategy="momentum")
    assert trade.asset_class is AssetClass.EQUITY


# _trade_to_order must carry the CRYPTO tag from the ProposedTrade onto the
# Order that reaches the risk engine + broker (the 24/7 exemptions key off it).
async def test_trade_to_order_carries_crypto_asset_class(stack) -> None:
    manager = stack["manager"]
    decision = Decision(
        action=DecisionAction.BUY,
        trades=[ProposedTrade(symbol="BTC/USD", side=OrderSide.BUY,
                              order_type=OrderType.MARKET, quantity=Decimal("0.05"),
                              strategy="momentum")],
        rationale=TradeRationale(
            thesis="t", timing="now", expected_edge="e", risk="r", reward="w",
            confidence=0.8, portfolio_impact="small", exit_plan=ExitPlan(),
            max_expected_loss="$100",
        ),
        cycle_id="cryptotest", created_at=datetime.now(UTC),
    )
    order = manager._trade_to_order(decision.trades[0], decision)
    assert order.asset_class is AssetClass.CRYPTO
    assert order.symbol == "BTC/USD"


def _spy_submit(broker: PaperBroker) -> list[Order]:
    """Record every order that actually reaches ``broker.submit_order`` (still
    delegating to the real fill). The adversarial invariant: a tripped breaker
    means this list stays empty for EVERY normal path."""
    calls: list[Order] = []
    real = broker.submit_order

    async def spy(order):
        calls.append(order)
        return await real(order)

    broker.submit_order = spy
    return calls


# Task 3 — adversarial "a tripped breaker rejects EVERY normal order".
# The halt-flatten carve-out (§3.4) admits ONLY kernel.halt()'s flatten path,
# which presents an engine-minted identity token. No /api/decision, /api/manual,
# or guardian path carries a token (no parameter exists on those entry points),
# so a tripped breaker must still block them all — including risk-reducing exits.


# execute_decision: a tripped breaker blocks BOTH an opening BUY and a
# risk-reducing SELL. Neither carries a halt token, so both are rejected before
# the broker is ever contacted — the AI cannot trade a halted book in any
# direction. The tooth is the empty submit-spy plus REJECTED_RISK on both.
async def test_tripped_breaker_blocks_execute_decision_buy_and_sell(stack) -> None:
    manager = stack["manager"]
    submit_calls = _spy_submit(stack["broker"])
    stack["risk"].circuit.force_open("operator HALT")

    buy = await manager.execute_decision(make_decision())
    sell = await manager.execute_decision(make_exit_decision())

    assert len(buy) == 1 and len(sell) == 1
    for order in (buy[0], sell[0]):
        assert order.status is OrderStatus.REJECTED_RISK, order.status
    assert submit_calls == [], "a halted breaker let an order reach the broker"


# submit_manual: the operator's own dashboard ticket is no exception — manual
# orders run the full risk gate, and a tripped breaker rejects them too (the
# operator's remedy is resume(), not a manual bypass).
async def test_tripped_breaker_blocks_submit_manual(stack) -> None:
    manager = stack["manager"]
    submit_calls = _spy_submit(stack["broker"])
    stack["risk"].circuit.force_open("operator HALT")

    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                  quantity=Decimal("10"), limit_price=Decimal("100.05"), strategy="manual")
    result = await manager.submit_manual(order)

    assert result.status is OrderStatus.REJECTED_RISK
    assert submit_calls == [], "a halted breaker let a manual order reach the broker"


# guardian dispatch: the guardian routes its stop/target exits through
# execute_decision (guardian.py:274), so a risk-reducing SELL on that path must
# also be blocked while the breaker is open — a resting/guardian exit gets NO
# carve-out (only kernel.halt()'s own flatten does, via a token).
async def test_tripped_breaker_blocks_guardian_dispatch(stack) -> None:
    manager = stack["manager"]
    submit_calls = _spy_submit(stack["broker"])
    stack["risk"].circuit.force_open("operator HALT")

    # The exact call the guardian makes: execute_decision(reduce-only exit).
    orders = await manager.execute_decision(make_exit_decision())

    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REJECTED_RISK
    assert submit_calls == [], "a halted breaker let a guardian exit reach the broker"


# The token thread: with an OPEN breaker, _submit's pre-submit re-check honors a
# live, engine-minted halt token for a leg-free risk-reducing exit — it does NOT
# reject with "halted before submit"; the exit reaches the broker and fills.
# This is the driver for the manager change (the reshaped predicate + kwarg);
# on pre-change code _submit ignores the token and rejects the valid exit.
async def test_submit_recheck_honors_active_token(stack) -> None:
    manager = stack["manager"]
    risk = stack["risk"]
    submit_calls = _spy_submit(stack["broker"])

    # Establish a real 10-share AAPL position so the reduce-only SELL is genuine
    # (broker.positions() shows the holding; the live reduce-only backstop passes).
    opened = await manager.execute_decision(make_decision())
    assert opened[0].status is OrderStatus.FILLED, opened[0].status
    submit_calls.clear()  # ignore the opening fill; watch only the halt exit

    # Now trip the breaker and mint the halt-flatten token, exactly as
    # kernel.halt() will (task 6). Only this path holds the token.
    risk.circuit.force_open("operator HALT")
    token = risk.open_halt_flatten_window()

    exit_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET,
                       quantity=Decimal("10"), strategy="halt_flatten")
    result = await manager._submit(exit_order, halt_token=token)

    # Tooth: the token carried the exit past the pre-submit breaker re-check.
    assert result.status is OrderStatus.FILLED, (result.status, result.status_reason)
    assert "halted before submit" not in (result.status_reason or "")
    assert submit_calls == [exit_order], "the token-bearing exit never reached the broker"


# --- manual trading over a delayed feed (the after-hours reality) --------------


async def _delayed_stack(tmp_path, age_seconds: float = 60.0):
    """The stack, rebuilt with a feed whose quotes are 60s old — DELAYED grade
    (past real_time_max_age, inside delayed_max_age). After-hours IEX looks
    exactly like this. The pair of tests below is the contract: the operator's
    own ticket may trade on it; the AI path may not."""
    bus = EventBus()
    router = DataRouter([(FakeProvider(name="feed", age_seconds=age_seconds), 10)], FreshnessPolicy())
    broker = PaperBroker(credentials={}, options={
        "starting_cash": "100000", "state_file": str(tmp_path / "paper-delayed.json")})
    broker.set_quote_fn(lambda s: router.quote(s, allow_delayed=True))
    await broker.connect()
    db = Database(tmp_path / "delayed.db")
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
    return {"manager": manager, "db": db, "bus": bus}


async def _close_delayed_stack(s) -> None:
    await s["bus"].close()
    await s["db"].close()


async def test_manual_order_trades_on_delayed_quote(tmp_path) -> None:
    # The operator at the ticket is looking at the market; a 60s-old reference
    # quote (after-hours reality on free feeds) must not reject their order.
    # Every OTHER risk rule still runs — only the freshness bound differs.
    s = await _delayed_stack(tmp_path)
    try:
        with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
            order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                          quantity=Decimal("10"), limit_price=Decimal("100.05"),
                          strategy="manual")
            result = await s["manager"].submit_manual(order)
        # FILLED, not merely "not rejected": the paper fill is synchronous and
        # deterministic here, and the stronger form also catches a stuck-
        # pending regression that never reaches the broker.
        assert result.status is OrderStatus.FILLED, result.status_reason
    finally:
        await _close_delayed_stack(s)


async def test_ai_decision_still_refuses_delayed_quote(tmp_path) -> None:
    # The discriminating twin: same 60s-old feed, same symbol — the AI path
    # keeps the live-only gate. Only the human at the ticket may accept
    # delayed data, because only the human is watching the market.
    s = await _delayed_stack(tmp_path)
    try:
        with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
            orders = await s["manager"].execute_decision(make_decision())
        assert len(orders) == 1
        assert orders[0].status is OrderStatus.REJECTED_RISK
        assert "delayed" in (orders[0].status_reason or "")
    finally:
        await _close_delayed_stack(s)


async def test_manual_order_still_refuses_stale_quote(tmp_path) -> None:
    # The carve-out accepts DELAYED only. Past delayed_max_age (900s) the
    # price is dead: sizing and price bands against it would be fiction, so
    # even the operator's own ticket refuses.
    s = await _delayed_stack(tmp_path, age_seconds=3600.0)
    try:
        with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
            order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                          quantity=Decimal("10"), limit_price=Decimal("100.05"),
                          strategy="manual")
            result = await s["manager"].submit_manual(order)
        assert result.status is OrderStatus.REJECTED_RISK
        assert "stale" in (result.status_reason or "")
    finally:
        await _close_delayed_stack(s)


async def test_approval_path_validates_strictly_no_manual_carveout(stack) -> None:
    # Neither the pre-approval gate nor the post-approval re-check may inherit
    # the manual ticket's delayed-quote carve-out: an approval can sit for
    # minutes while the human decides, and the re-check exists precisely to
    # re-anchor the order to live data.
    stack["manager"].set_mode(TradingMode.APPROVAL)
    stack["approvals"].wait = AsyncMock(return_value=True)
    seen: list[bool] = []

    async def spy(order, *, halt_token=None, allow_delayed=False):
        seen.append(allow_delayed)
        return  # pass both gates; the strictness of the CALL is the tooth

    stack["risk"].validate_order = spy

    await stack["manager"].execute_decision(make_decision())

    assert seen == [False, False]  # pre-approval gate + post-approval re-check
