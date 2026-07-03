"""Order manager: the only path from a decision to a broker.

Responsibilities:
  * translate AI decisions into orders and persist them before submission
    (crash between persist and submit is reconciled at startup via the
    broker's client-order-id lookup — never a double submit);
  * enforce operating mode (research: never submit; approval: human gate;
    autonomous: submit within risk limits);
  * run the risk engine on every order, no exceptions;
  * submit with bounded retries on retryable broker errors;
  * poll open orders to terminal state and publish fill/reject events;
  * feed execution errors to the circuit breaker.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import structlog

from ..brokers.base import Broker
from ..core.enums import OrderStatus, TradingMode
from ..core.errors import (
    BrokerError,
    CircuitBreakerOpen,
    DataError,
    DuplicateOrderError,
    RiskViolation,
)
from ..core.events import EventBus, Topics
from ..core.models import Decision, Order, ProposedTrade
from ..risk.engine import RiskEngine
from ..security.audit import AuditLog
from ..storage.db import Database
from .approvals import ApprovalQueue

log = structlog.get_logger(__name__)

_SUBMIT_RETRIES = 3
_POLL_INTERVAL = 5.0
_POLL_TIMEOUT = 8 * 60 * 60  # give a DAY order the whole session


class OrderManager:
    def __init__(self, broker: Broker, risk: RiskEngine, approvals: ApprovalQueue,
                 db: Database, audit: AuditLog, bus: EventBus, *, mode: TradingMode) -> None:
        self._broker = broker
        self._risk = risk
        self._approvals = approvals
        self._db = db
        self._audit = audit
        self._bus = bus
        self._mode = mode
        self._poll_tasks: set[asyncio.Task[None]] = set()

    @property
    def mode(self) -> TradingMode:
        return self._mode

    def set_mode(self, mode: TradingMode) -> None:
        self._mode = mode

    # -- entry point --------------------------------------------------------------

    async def execute_decision(self, decision: Decision) -> list[Order]:
        """Process every proposed trade in a decision. Returns final orders
        (which may be risk-rejected, human-rejected, or submitted)."""
        results: list[Order] = []
        for trade in decision.trades:
            order = self._trade_to_order(trade, decision)
            results.append(await self._process_order(order, decision))
        return results

    def _trade_to_order(self, trade: ProposedTrade, decision: Decision) -> Order:
        return Order(
            symbol=trade.symbol.upper(),
            asset_class=trade.asset_class,
            side=trade.side,
            order_type=trade.order_type,
            quantity=trade.quantity,
            limit_price=trade.limit_price,
            stop_price=trade.stop_price,
            time_in_force=trade.time_in_force,
            legs=trade.legs,
            strategy=trade.strategy,
            decision_id=decision.id,
            created_at=datetime.now(UTC),
        )

    async def _process_order(self, order: Order, decision: Decision) -> Order:
        await self._persist(order)

        if self._mode is TradingMode.RESEARCH:
            order.status = OrderStatus.REJECTED_HUMAN
            order.status_reason = "research mode: orders are never submitted"
            await self._persist(order)
            return order

        # Risk gate — always, in every mode.
        try:
            await self._risk.validate_order(order)
        except (RiskViolation, CircuitBreakerOpen) as exc:
            order.status = OrderStatus.REJECTED_RISK
            order.status_reason = str(exc)
            await self._persist(order)
            await self._audit.append("risk", "order.rejected",
                                     {"order_id": order.id, "reason": str(exc)})
            await self._bus.publish(Topics.ORDER_REJECTED,
                                    {"order": order.model_dump(mode="json"), "reason": str(exc)})
            return order
        except DataError as exc:
            order.status = OrderStatus.REJECTED_RISK
            order.status_reason = f"required live data unavailable: {exc}"
            await self._persist(order)
            await self._bus.publish(Topics.ORDER_REJECTED,
                                    {"order": order.model_dump(mode="json"), "reason": str(exc)})
            return order

        # Human gate in approval mode.
        if self._mode is TradingMode.APPROVAL:
            order.status = OrderStatus.PENDING_APPROVAL
            await self._persist(order)
            entry = await self._approvals.request(order, decision)
            approved = await self._approvals.wait(entry)
            if not approved:
                order.status = OrderStatus.REJECTED_HUMAN
                order.status_reason = f"not approved ({entry.resolver})"
                await self._persist(order)
                await self._audit.append("human", "order.rejected",
                                         {"order_id": order.id, "resolver": entry.resolver})
                await self._bus.publish(Topics.ORDER_REJECTED,
                                        {"order": order.model_dump(mode="json"),
                                         "reason": order.status_reason})
                return order
            await self._audit.append("human", "order.approved", {"order_id": order.id})
            # Re-validate: conditions may have moved while the human decided.
            try:
                await self._risk.validate_order(order)
            except (RiskViolation, CircuitBreakerOpen, DataError) as exc:
                order.status = OrderStatus.REJECTED_RISK
                order.status_reason = f"post-approval re-check failed: {exc}"
                await self._persist(order)
                return order

        order.status = OrderStatus.APPROVED
        await self._persist(order)
        return await self._submit(order)

    # -- submission ------------------------------------------------------------------

    async def _submit(self, order: Order) -> Order:
        await self._guard_duplicate(order)
        last_error: Exception | None = None
        for attempt in range(1, _SUBMIT_RETRIES + 1):
            try:
                order = await self._broker.submit_order(order)
                break
            except BrokerError as exc:
                last_error = exc
                self._risk.note_execution_error(str(exc))
                if not exc.retryable or attempt == _SUBMIT_RETRIES:
                    order.status = OrderStatus.ERROR if exc.retryable else OrderStatus.REJECTED_BROKER
                    order.status_reason = str(exc)
                    await self._persist(order)
                    await self._audit.append("system", "order.submit_failed",
                                             {"order_id": order.id, "error": str(exc)})
                    await self._bus.publish(Topics.ORDER_REJECTED,
                                            {"order": order.model_dump(mode="json"),
                                             "reason": str(exc)})
                    return order
                await asyncio.sleep(2 ** attempt)
        else:  # pragma: no cover — loop always breaks or returns
            raise AssertionError(str(last_error))

        self._risk.note_order_submitted(order.symbol)
        await self._persist(order)
        await self._audit.append("system", "order.submitted", {
            "order_id": order.id, "broker": order.broker,
            "broker_order_id": order.broker_order_id,
            "symbol": order.symbol, "side": order.side, "qty": str(order.quantity),
        })
        await self._bus.publish(Topics.ORDER_UPDATED, {"order": order.model_dump(mode="json")})
        self._spawn_poller(order)
        return order

    async def _guard_duplicate(self, order: Order) -> None:
        row = await self._db.fetch_one(
            "SELECT status FROM orders WHERE client_order_id = ? AND id != ?",
            (order.client_order_id, order.id),
        )
        if row is not None:
            raise DuplicateOrderError(f"client_order_id {order.client_order_id} already used")
        # Same-cycle guard: identical open order (symbol+side+qty) at the broker.
        for open_order in await self._safe_open_orders():
            if (open_order.symbol == order.symbol and open_order.side == order.side
                    and open_order.quantity == order.quantity):
                raise DuplicateOrderError(
                    f"an identical open order for {order.symbol} already exists at the broker"
                )

    async def _safe_open_orders(self) -> list[Order]:
        try:
            return await self._broker.open_orders()
        except BrokerError as exc:
            # Can't verify → don't trade. Duplicate prevention must not be skipped.
            raise DuplicateOrderError(f"cannot verify open orders at broker: {exc}") from exc

    # -- lifecycle polling ---------------------------------------------------------------

    def _spawn_poller(self, order: Order) -> None:
        task = asyncio.create_task(self._poll_to_terminal(order), name=f"order-poll-{order.id[:8]}")
        self._poll_tasks.add(task)
        task.add_done_callback(self._poll_tasks.discard)

    async def _poll_to_terminal(self, order: Order) -> None:
        deadline = asyncio.get_running_loop().time() + _POLL_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                order = await self._broker.order_status(order)
            except BrokerError as exc:
                log.warning("order status poll failed", order_id=order.id, error=str(exc))
                continue
            await self._persist(order)
            if order.status.is_terminal:
                topic = Topics.ORDER_FILLED if order.status is OrderStatus.FILLED else Topics.ORDER_UPDATED
                await self._audit.append("system", f"order.{order.status.value}", {
                    "order_id": order.id,
                    "filled_qty": str(order.filled_quantity),
                    "avg_price": str(order.avg_fill_price) if order.avg_fill_price else None,
                })
                await self._bus.publish(topic, {"order": order.model_dump(mode="json")})
                return
        log.warning("order poll timed out; leaving order open", order_id=order.id)

    async def resume_open_orders(self) -> int:
        """Crash recovery: re-attach pollers to orders that were open at the
        broker when the process died."""
        rows = await self._db.fetch_all(
            "SELECT payload FROM orders WHERE status IN (?, ?, ?)",
            (OrderStatus.SUBMITTED.value, OrderStatus.ACCEPTED.value,
             OrderStatus.PARTIALLY_FILLED.value),
        )
        count = 0
        for (payload,) in rows:
            order = Order.model_validate(json.loads(payload))
            self._spawn_poller(order)
            count += 1
        if count:
            log.info("resumed polling for open orders", count=count)
        return count

    async def cancel(self, order_id: str) -> Order:
        row = await self._db.fetch_one("SELECT payload FROM orders WHERE id = ?", (order_id,))
        if row is None:
            raise KeyError(f"unknown order {order_id}")
        order = Order.model_validate(json.loads(row[0]))
        order = await self._broker.cancel_order(order)
        await self._persist(order)
        await self._audit.append("human", "order.canceled", {"order_id": order.id})
        await self._bus.publish(Topics.ORDER_UPDATED, {"order": order.model_dump(mode="json")})
        return order

    # -- persistence -----------------------------------------------------------------------

    async def _persist(self, order: Order) -> None:
        order.updated_at = datetime.now(UTC)
        if order.created_at is None:
            order.created_at = order.updated_at
        payload = json.dumps(order.model_dump(mode="json"))
        await self._db.execute(
            "INSERT INTO orders (id, client_order_id, broker, broker_order_id, payload, status, "
            "decision_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, status=excluded.status, "
            "broker_order_id=excluded.broker_order_id, updated_at=excluded.updated_at",
            (order.id, order.client_order_id, order.broker, order.broker_order_id, payload,
             order.status.value, order.decision_id,
             order.created_at.isoformat(), order.updated_at.isoformat()),
        )

    async def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT payload FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [json.loads(r[0]) for r in rows]

    async def orders_today_count(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        row = await self._db.fetch_one(
            "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND status NOT IN (?, ?, ?)",
            (today, OrderStatus.REJECTED_RISK.value, OrderStatus.REJECTED_HUMAN.value,
             OrderStatus.PROPOSED.value),
        )
        return int(row[0]) if row else 0
