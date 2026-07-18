"""Halt-cleanup behaviour on ``OrderManager`` (control-hardening spec §3.2).

Task 4 — ``cancel_all_open``: on halt, every resting broker order is canceled
exactly once (never retried), a cancel failure is recorded and the loop
continues, cross-broker rows are skipped (never canceled against the wrong
brokerage), and the pass returns a frozen ``HaltCleanupSummary``.

These pins guard the real-money control plane: a resting order left live during
a halt can fill mid-halt, and a retry loop on a failing cancel could hammer the
broker or wedge the halt. The behaviour under test is deterministic — no LLM,
no risk-rule exemption, Decimal money — and each consequential action audits.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from poseidon.core.enums import OrderSide, OrderStatus, OrderType, TradingMode
from poseidon.core.errors import BrokerError
from poseidon.core.events import EventBus
from poseidon.core.models import Order
from poseidon.execution.manager import HaltCleanupSummary, OrderManager
from poseidon.security.audit import AuditLog
from poseidon.storage.db import Database


class FakeBroker:
    """A minimal broker whose ``cancel_order`` is fully controllable: it counts
    every call (to prove exactly-once) and can be made to raise for chosen order
    ids (to prove failures are recorded, not retried)."""

    account_scope = "fake:paper"

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.cancel_calls: list[str] = []
        self.fail_ids: set[str] = set()

    async def cancel_order(self, order: Order) -> Order:
        self.cancel_calls.append(order.id)
        if order.id in self.fail_ids:
            raise BrokerError(self.name, f"cancel rejected for {order.id}", retryable=True)
        order.status = OrderStatus.CANCELED
        return order


@pytest.fixture
async def harness(tmp_path):
    """Real DB + AuditLog + EventBus with a controllable fake broker. The risk
    engine and approval queue are irrelevant to cancel_all_open (it never
    touches them), so they are stubbed."""
    bus = EventBus()
    db = Database(tmp_path / "halt.db")
    await db.open()
    audit = AuditLog(db)
    broker = FakeBroker()
    manager = OrderManager(broker, MagicMock(), MagicMock(), db, audit, bus,
                           mode=TradingMode.AUTONOMOUS)
    yield {"manager": manager, "broker": broker, "db": db, "audit": audit, "bus": bus}
    await bus.close()
    await db.close()


async def _seed_open(manager: OrderManager, *, symbol: str, broker: str,
                     status: OrderStatus = OrderStatus.SUBMITTED,
                     qty: str = "10") -> Order:
    """Persist an order the DB will report as live at the broker."""
    order = Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT,
                  quantity=Decimal(qty), limit_price=Decimal("100.00"),
                  strategy="momentum", broker=broker, status=status)
    await manager._persist(order)
    return order


def _actions(audit_calls: list[tuple[str, str, dict]]) -> list[str]:
    return [action for (_actor, action, _payload) in audit_calls]


def _spy_audit(audit: AuditLog) -> list[tuple[str, str, dict]]:
    calls: list[tuple[str, str, dict]] = []
    real = audit.append

    async def spy(actor, action, payload=None):
        calls.append((actor, action, payload or {}))
        return await real(actor, action, payload)

    audit.append = spy
    return calls


# -- test_cancels_each_open_order_once ---------------------------------------------

async def test_cancels_each_open_order_once(harness) -> None:
    manager, broker = harness["manager"], harness["broker"]
    a = await _seed_open(manager, symbol="AAPL", broker="fake",
                         status=OrderStatus.SUBMITTED)
    b = await _seed_open(manager, symbol="MSFT", broker="fake",
                         status=OrderStatus.ACCEPTED)
    c = await _seed_open(manager, symbol="NVDA", broker="fake",
                         status=OrderStatus.PARTIALLY_FILLED)

    summary = await manager.cancel_all_open(reason="operator HALT")

    # Every live row canceled, each exactly once (no retries, no double-cancel).
    assert sorted(broker.cancel_calls) == sorted([a.id, b.id, c.id])
    assert len(broker.cancel_calls) == 3
    assert set(summary.canceled) == {a.id, b.id, c.id}
    assert summary.failed == []
    assert summary.skipped == []
    # The DB rows are now CANCELED (persisted).
    for order in (a, b, c):
        row = await harness["db"].fetch_one("SELECT status FROM orders WHERE id = ?", (order.id,))
        assert row[0] == OrderStatus.CANCELED.value


# -- test_cancel_failure_recorded_not_retried --------------------------------------

async def test_cancel_failure_recorded_not_retried(harness) -> None:
    manager, broker = harness["manager"], harness["broker"]
    audit_calls = _spy_audit(harness["audit"])
    bad = await _seed_open(manager, symbol="AAPL", broker="fake")
    good = await _seed_open(manager, symbol="MSFT", broker="fake")
    broker.fail_ids = {bad.id}

    summary = await manager.cancel_all_open(reason="operator HALT")

    # The failing cancel was attempted EXACTLY ONCE — no retry loop.
    assert broker.cancel_calls.count(bad.id) == 1
    # It is recorded in the failure list with order_id/symbol/error…
    assert len(summary.failed) == 1
    failure = summary.failed[0]
    assert failure["order_id"] == bad.id
    assert failure["symbol"] == "AAPL"
    assert failure["error"]
    # …and audited as a system halt.cancel_failed fact.
    assert any(
        actor == "system" and action == "halt.cancel_failed"
        and payload.get("order_id") == bad.id
        for (actor, action, payload) in audit_calls
    ), f"expected a halt.cancel_failed audit; got {audit_calls}"
    # The loop CONTINUED: the healthy order was still canceled.
    assert summary.canceled == [good.id]
    assert broker.cancel_calls.count(good.id) == 1


# -- test_cross_broker_rows_skipped_and_recorded -----------------------------------

async def test_cross_broker_rows_skipped_and_recorded(harness) -> None:
    manager, broker = harness["manager"], harness["broker"]
    mine = await _seed_open(manager, symbol="AAPL", broker="fake")
    theirs = await _seed_open(manager, symbol="MSFT", broker="other_brokerage")

    summary = await manager.cancel_all_open(reason="operator HALT")

    # A cross-broker order is NEVER canceled against the active broker…
    assert theirs.id not in broker.cancel_calls
    # …but it IS recorded so the operator knows it survived the halt.
    assert summary.skipped == [theirs.id]
    # The active-broker order was canceled.
    assert broker.cancel_calls == [mine.id]
    assert summary.canceled == [mine.id]


# -- test_returns_summary ----------------------------------------------------------

async def test_returns_summary(harness) -> None:
    manager = harness["manager"]

    summary = await manager.cancel_all_open(reason="operator HALT")

    # A frozen dataclass with exactly canceled / failed / skipped.
    assert isinstance(summary, HaltCleanupSummary)
    assert is_dataclass(summary)
    assert {f.name for f in fields(summary)} == {"canceled", "failed", "skipped"}
    # Frozen: fields cannot be reassigned.
    with pytest.raises(FrozenInstanceError):
        summary.canceled = ["x"]  # type: ignore[misc]
    # Empty book → empty summary (no crash, no broker call).
    assert summary.canceled == []
    assert summary.failed == []
    assert summary.skipped == []


# -- switching guard (spec §3.2: "if _switching → abort cleanup, record") ----------

async def test_switching_aborts_cleanup_without_canceling(harness) -> None:
    manager, broker = harness["manager"], harness["broker"]
    audit_calls = _spy_audit(harness["audit"])
    await _seed_open(manager, symbol="AAPL", broker="fake")
    manager._switching = True

    summary = await manager.cancel_all_open(reason="operator HALT")

    # A mid-flight broker swap means the book's broker is indeterminate — cancel
    # nothing rather than cancel against the wrong brokerage.
    assert broker.cancel_calls == []
    assert summary.canceled == []
    assert "halt.cleanup_failed" in _actions(audit_calls)
