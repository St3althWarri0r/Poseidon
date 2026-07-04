"""Position guardian: enforces exit plans between review cycles.

Every AI decision that opens a position carries an exit plan (stop loss /
take profit). Without enforcement those numbers are just prose — a gap
between review cycles where nothing watches the position. The guardian
closes that gap:

  * when an entry order fills, the decision's exit plan is persisted per
    symbol;
  * on a short interval during market hours, each active plan is checked
    against a live, freshness-graded quote;
  * a breach produces an exit through the normal order-manager path — so
    it obeys the operating mode (research: notify only; approval: queued
    for the human; autonomous: executed) and still passes the risk engine;
  * plans deactivate when the position is gone, and a triggered plan does
    not retrigger.

The guardian enforces numeric stops/targets. ``time_stop`` is free text
by design (e.g. "exit before earnings") and remains the AI's job during
review cycles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import structlog

from ..core.config import GuardianConfig
from ..core.enums import DecisionAction, MarketSession, OrderSide, OrderType, TradingMode
from ..core.errors import DataError
from ..core.events import Topics
from ..core.models import Decision, ExitPlan, Order, ProposedTrade, TradeRationale
from ..storage.db import Database

log = structlog.get_logger(__name__)


class PositionGuardian:
    def __init__(self, config: GuardianConfig, db: Database, kernel: object) -> None:
        # `kernel` is the ApplicationKernel; typed loosely to avoid an import
        # cycle. The guardian uses: router, portfolio, order_manager, clock,
        # bus, audit.
        self._config = config
        self._db = db
        self._kernel = kernel

    # -- plan registration (wired to ORDER_FILLED events) ----------------------

    async def on_order_filled(self, _topic: str, payload: object) -> None:
        order_data = (payload or {}).get("order", {}) if isinstance(payload, dict) else {}
        if not order_data:
            return
        try:
            order = Order.model_validate(order_data)
        except Exception:
            return
        if order.side.is_buy and order.decision_id:
            await self._register_plan_for(order)
        elif order.side.is_risk_reducing:
            await self._maybe_deactivate(order.symbol, "position reduced/closed")

    async def _register_plan_for(self, order: Order) -> None:
        row = await self._db.fetch_one(
            "SELECT payload FROM decisions WHERE id = ?", (order.decision_id,)
        )
        if row is None:
            return
        import json

        decision = json.loads(row[0])
        trades = decision.get("trades") or []
        rationale = decision.get("rationale") or {}
        decision_exit = rationale.get("exit_plan") or {}
        symbol_up = order.symbol.upper()

        # Attribute exit levels to THIS symbol only. Prefer the matching
        # trade's own stop/target; fall back to the decision-level exit plan
        # ONLY when the decision opened a single position (so there is no
        # ambiguity about whose stop it is). A multi-symbol decision with no
        # per-trade levels arms nothing rather than risk applying one
        # symbol's stop to another (which would force-sell a fresh position).
        matching = next((t for t in trades if str(t.get("symbol", "")).upper() == symbol_up), None)
        stop = target = None
        if matching is not None and (matching.get("stop_loss") or matching.get("take_profit")):
            stop = matching.get("stop_loss")
            target = matching.get("take_profit")
        elif len([t for t in trades if str(t.get("side", "")).startswith("buy")]) <= 1:
            stop = decision_exit.get("stop_loss")
            target = decision_exit.get("take_profit")
        else:
            log.warning("multi-symbol decision without per-trade exit levels; not arming",
                        symbol=order.symbol, decision_id=order.decision_id)
            return
        if stop is None and target is None:
            return  # nothing enforceable
        exit_plan = decision_exit  # time_stop (free text) is decision-level
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO exit_plans (symbol, decision_id, stop_loss, take_profit, time_stop, "
            "quantity, active, triggered_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, NULL, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET decision_id=excluded.decision_id, "
            "stop_loss=excluded.stop_loss, take_profit=excluded.take_profit, "
            "time_stop=excluded.time_stop, quantity=excluded.quantity, active=1, "
            "triggered_reason=NULL, updated_at=excluded.updated_at",
            (order.symbol.upper(), order.decision_id, stop, target,
             exit_plan.get("time_stop"), str(order.filled_quantity or order.quantity),
             now, now),
        )
        log.info("exit plan armed", symbol=order.symbol, stop=stop, target=target)

    async def _maybe_deactivate(self, symbol: str, reason: str) -> None:
        portfolio = self._kernel.portfolio  # type: ignore[attr-defined]
        if portfolio.position_for(symbol) is None:
            await self._db.execute(
                "UPDATE exit_plans SET active = 0, triggered_reason = ?, updated_at = ? "
                "WHERE symbol = ? AND active = 1",
                (reason, datetime.now(UTC).isoformat(), symbol.upper()),
            )

    # -- the watch loop (scheduler job) ------------------------------------------

    async def check_all(self) -> None:
        if not self._config.enabled:
            return
        kernel = self._kernel
        if kernel.clock.session() is not MarketSession.REGULAR:  # type: ignore[attr-defined]
            return
        rows = await self._db.fetch_all(
            "SELECT symbol, decision_id, stop_loss, take_profit FROM exit_plans WHERE active = 1"
        )
        for symbol, decision_id, stop_raw, target_raw in rows:
            position = kernel.portfolio.position_for(symbol)  # type: ignore[attr-defined]
            if position is None or position.quantity <= 0:
                await self._maybe_deactivate(symbol, "position no longer held")
                continue
            try:
                quote = await kernel.router.quote(symbol, allow_delayed=False)  # type: ignore[attr-defined]
            except DataError as exc:
                log.warning("guardian cannot price position; will retry",
                            symbol=symbol, error=str(exc))
                continue
            price = quote.bid or quote.mid or quote.last
            if price is None:
                continue
            stop = Decimal(stop_raw) if stop_raw else None
            target = Decimal(target_raw) if target_raw else None
            breach: str | None = None
            if stop is not None and price <= stop:
                breach = f"stop loss: {symbol} at {price} <= stop {stop}"
            elif target is not None and price >= target:
                breach = f"take profit: {symbol} at {price} >= target {target}"
            if breach:
                await self._trigger_exit(symbol, decision_id, position.quantity, price, breach)

    async def _trigger_exit(self, symbol: str, decision_id: str, quantity: Decimal,
                            price: Decimal, reason: str) -> None:
        kernel = self._kernel
        now = datetime.now(UTC).isoformat()
        # Latch first so a slow/failed exit cannot fire once per tick forever;
        # the outcome (fill or rejection) is notified and audited either way.
        await self._db.execute(
            "UPDATE exit_plans SET active = 0, triggered_reason = ?, updated_at = ? WHERE symbol = ?",
            (reason, now, symbol.upper()),
        )
        await kernel.audit.append("guardian", "exit.triggered",  # type: ignore[attr-defined]
                                  {"symbol": symbol, "reason": reason, "price": str(price)})
        log.warning("guardian exit triggered", symbol=symbol, reason=reason)

        mode: TradingMode = kernel.order_manager.mode  # type: ignore[attr-defined]
        if mode is TradingMode.RESEARCH:
            await kernel.bus.publish(Topics.NOTIFY, {  # type: ignore[attr-defined]
                "level": "warning", "title": f"Exit level hit: {symbol}",
                "body": f"{reason}. Research mode — no order placed; review the position.",
            })
            return

        decision = Decision(
            action=DecisionAction.SELL,
            trades=[ProposedTrade(
                symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT,
                quantity=quantity, limit_price=price, strategy="guardian",
            )],
            rationale=TradeRationale(
                thesis=f"Exit-plan enforcement: {reason}.",
                timing="The pre-committed exit level from the original decision was reached.",
                expected_edge="Discipline: realizing the planned exit rather than drifting.",
                risk="Exit executes at the live bid; slippage bounded by the risk engine.",
                reward="Caps the loss / locks in the gain the original plan specified.",
                confidence=1.0,
                supporting_indicators=[reason],
                portfolio_impact=f"Closes the {symbol} position ({quantity} units).",
                exit_plan=ExitPlan(notes="this order IS the exit"),
                max_expected_loss="bounded by the stop level already reached",
            ),
            data_sources=["guardian"],
            model="guardian",
            cycle_id=f"guardian-{decision_id[:8]}",
            created_at=datetime.now(UTC),
        )
        orders = await kernel.order_manager.execute_decision(decision)  # type: ignore[attr-defined]
        for order in orders:
            await kernel.bus.publish(Topics.NOTIFY, {  # type: ignore[attr-defined]
                "level": "warning", "title": f"Guardian exit: {symbol}",
                "body": f"{reason}\nOrder status: {order.status.value}"
                        + (f" — {order.status_reason}" if order.status_reason else ""),
            })

    async def active_plans(self) -> list[dict[str, object]]:
        rows = await self._db.fetch_all(
            "SELECT symbol, stop_loss, take_profit, time_stop, quantity, created_at "
            "FROM exit_plans WHERE active = 1 ORDER BY symbol"
        )
        return [
            {"symbol": r[0], "stop_loss": r[1], "take_profit": r[2],
             "time_stop": r[3], "quantity": r[4], "created_at": r[5]}
            for r in rows
        ]
