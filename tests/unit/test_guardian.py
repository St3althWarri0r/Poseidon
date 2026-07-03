"""Position guardian: arming, breach detection, mode-aware exits, latching."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aegis_trader.core.config import GuardianConfig
from aegis_trader.core.enums import (
    DecisionAction,
    MarketSession,
    OrderSide,
    OrderStatus,
    TradingMode,
)
from aegis_trader.core.models import Order, Position, Quote
from aegis_trader.execution.guardian import PositionGuardian
from aegis_trader.storage.db import Database


class KernelStub:
    """Just the surface the guardian touches."""

    def __init__(self, *, mode: TradingMode, price: str, position_qty: str | None) -> None:
        self.mode = mode
        self._price = Decimal(price)
        self._position_qty = position_qty
        self.executed_decisions: list = []
        self.notifications: list[dict] = []
        self.audit_entries: list[tuple[str, str]] = []

        self.clock = SimpleNamespace(session=lambda: MarketSession.REGULAR)
        self.order_manager = SimpleNamespace(
            mode=mode, execute_decision=self._execute_decision
        )
        self.audit = SimpleNamespace(append=self._audit_append)
        self.bus = SimpleNamespace(publish=self._publish)
        self.router = SimpleNamespace(quote=self._quote)
        self.portfolio = SimpleNamespace(position_for=self._position_for)

    async def _execute_decision(self, decision):
        self.executed_decisions.append(decision)
        return [Order(symbol=decision.trades[0].symbol, side=OrderSide.SELL,
                      quantity=decision.trades[0].quantity, status=OrderStatus.FILLED)]

    async def _audit_append(self, actor: str, action: str, payload=None):
        self.audit_entries.append((actor, action))

    async def _publish(self, topic: str, payload=None):
        if topic == "notify":
            self.notifications.append(payload)

    async def _quote(self, symbol: str, allow_delayed: bool = False) -> Quote:
        return Quote(symbol=symbol, bid=self._price, ask=self._price + Decimal("0.10"),
                     as_of=datetime.now(UTC), source="stub")

    def _position_for(self, symbol: str):
        if self._position_qty is None:
            return None
        return Position(symbol=symbol, quantity=Decimal(self._position_qty),
                        avg_entry_price=Decimal("100"), broker="stub",
                        as_of=datetime.now(UTC))


async def _db_with_decision(tmp_path, *, stop: str | None, target: str | None) -> Database:
    db = Database(tmp_path / "g.db")
    await db.open()
    decision_payload = {
        "rationale": {"exit_plan": {"stop_loss": stop, "take_profit": target}}
    }
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        ("dec1", "c1", "buy", json.dumps(decision_payload), datetime.now(UTC).isoformat()),
    )
    return db


def filled_buy(symbol: str = "AAPL", qty: str = "10") -> dict:
    order = Order(symbol=symbol, side=OrderSide.BUY, quantity=Decimal(qty),
                  decision_id="dec1", status=OrderStatus.FILLED,
                  filled_quantity=Decimal(qty))
    return {"order": order.model_dump(mode="json")}


async def test_fill_arms_exit_plan(tmp_path) -> None:
    db = await _db_with_decision(tmp_path, stop="95", target="120")
    kernel = KernelStub(mode=TradingMode.AUTONOMOUS, price="100", position_qty="10")
    guardian = PositionGuardian(GuardianConfig(), db, kernel)
    await guardian.on_order_filled("order.filled", filled_buy())
    plans = await guardian.active_plans()
    assert plans == [pytest.approx(plans[0])]  # exactly one
    assert plans[0]["symbol"] == "AAPL" and plans[0]["stop_loss"] == "95"
    await db.close()


async def test_no_plan_when_nothing_enforceable(tmp_path) -> None:
    db = await _db_with_decision(tmp_path, stop=None, target=None)
    kernel = KernelStub(mode=TradingMode.AUTONOMOUS, price="100", position_qty="10")
    guardian = PositionGuardian(GuardianConfig(), db, kernel)
    await guardian.on_order_filled("order.filled", filled_buy())
    assert await guardian.active_plans() == []
    await db.close()


async def test_stop_breach_executes_exit_in_autonomous(tmp_path) -> None:
    db = await _db_with_decision(tmp_path, stop="95", target="120")
    kernel = KernelStub(mode=TradingMode.AUTONOMOUS, price="94.50", position_qty="10")
    guardian = PositionGuardian(GuardianConfig(), db, kernel)
    await guardian.on_order_filled("order.filled", filled_buy())
    await guardian.check_all()
    assert len(kernel.executed_decisions) == 1
    decision = kernel.executed_decisions[0]
    assert decision.action is DecisionAction.SELL
    assert decision.trades[0].symbol == "AAPL"
    assert decision.trades[0].quantity == Decimal("10")
    assert decision.rationale is not None and "stop loss" in decision.rationale.thesis
    # Latched: plan no longer active, second sweep does nothing.
    assert await guardian.active_plans() == []
    await guardian.check_all()
    assert len(kernel.executed_decisions) == 1
    assert ("guardian", "exit.triggered") in kernel.audit_entries
    await db.close()


async def test_target_breach_triggers(tmp_path) -> None:
    db = await _db_with_decision(tmp_path, stop="95", target="120")
    kernel = KernelStub(mode=TradingMode.AUTONOMOUS, price="121", position_qty="10")
    guardian = PositionGuardian(GuardianConfig(), db, kernel)
    await guardian.on_order_filled("order.filled", filled_buy())
    await guardian.check_all()
    assert len(kernel.executed_decisions) == 1
    assert "take profit" in kernel.executed_decisions[0].rationale.thesis
    await db.close()


async def test_no_breach_no_action(tmp_path) -> None:
    db = await _db_with_decision(tmp_path, stop="95", target="120")
    kernel = KernelStub(mode=TradingMode.AUTONOMOUS, price="100", position_qty="10")
    guardian = PositionGuardian(GuardianConfig(), db, kernel)
    await guardian.on_order_filled("order.filled", filled_buy())
    await guardian.check_all()
    assert kernel.executed_decisions == []
    assert len(await guardian.active_plans()) == 1
    await db.close()


async def test_research_mode_notifies_without_order(tmp_path) -> None:
    db = await _db_with_decision(tmp_path, stop="95", target=None)
    kernel = KernelStub(mode=TradingMode.RESEARCH, price="90", position_qty="10")
    guardian = PositionGuardian(GuardianConfig(), db, kernel)
    await guardian.on_order_filled("order.filled", filled_buy())
    await guardian.check_all()
    assert kernel.executed_decisions == []
    assert kernel.notifications and "Research mode" in kernel.notifications[0]["body"]
    await db.close()


async def test_plan_deactivates_when_position_gone(tmp_path) -> None:
    db = await _db_with_decision(tmp_path, stop="95", target="120")
    kernel = KernelStub(mode=TradingMode.AUTONOMOUS, price="100", position_qty="10")
    guardian = PositionGuardian(GuardianConfig(), db, kernel)
    await guardian.on_order_filled("order.filled", filled_buy())
    kernel._position_qty = None  # position closed externally / by the AI
    await guardian.check_all()
    assert await guardian.active_plans() == []
    assert kernel.executed_decisions == []
    await db.close()
