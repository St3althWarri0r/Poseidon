"""Built-in paper trading broker.

A full local simulation used for paper mode, integration tests, historical
replay, and backtesting. Fills are priced from *live* quotes supplied by
the data router (injected as ``options["quote_fn"]``) — even the simulator
honors the no-fabricated-prices rule. State persists to a JSON file so a
restart never loses the simulated account.

Fill model:
  * market orders fill immediately at ask (buy) / bid (sell), falling back
    to last/mid when the book is one-sided;
  * limit orders fill when marketable against the current quote;
  * non-marketable limit orders rest and are re-evaluated by
    ``order_status`` / ``open_orders`` polls (the order manager polls).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ...core.enums import BrokerCapability, OrderStatus
from ...core.errors import BrokerError, OrderRejectedError
from ...core.models import AccountSnapshot, Fill, Order, Position, Quote, TaxLot
from ..base import Broker

QuoteFn = Callable[[str], Awaitable[Quote]]

_DEFAULT_CASH = Decimal("100000")


class PaperBroker(Broker):
    name = "paper"
    display_name = "Poseidon Paper Trading"

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        super().__init__(credentials=credentials, paper=True, timeout=timeout, options=options)
        self._quote_fn: QuoteFn | None = self._options.get("quote_fn")
        state_file = self._options.get("state_file")
        self._state_file: Path | None = Path(state_file) if state_file else None
        # A read-only instance (the Account view's connection test) may load
        # the shared state file but must never write it back — its stale
        # snapshot would clobber the ACTIVE paper broker's saved state.
        self._read_only = bool(self._options.get("read_only", False))
        self._cash = Decimal(str(self._options.get("starting_cash", _DEFAULT_CASH)))
        self._positions: dict[str, dict[str, Any]] = {}  # symbol -> {qty, avg_price}
        self._lots: list[dict[str, Any]] = []
        self._open_orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._realized_pnl_today = Decimal(0)

    def set_quote_fn(self, fn: QuoteFn) -> None:
        """Wired by the kernel after the data router exists."""
        self._quote_fn = fn

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset(
            {
                BrokerCapability.EQUITIES,
                BrokerCapability.FRACTIONAL_SHARES,
                BrokerCapability.EXTENDED_HOURS,
                BrokerCapability.PAPER_TRADING,
                BrokerCapability.TAX_LOTS,
            }
        )

    async def connect(self) -> None:
        self._load_state()
        self._connected = True

    async def disconnect(self) -> None:
        self._save_state()
        await super().disconnect()

    async def ping(self) -> bool:
        return True

    # -- account -----------------------------------------------------------------

    async def account(self) -> AccountSnapshot:
        equity = self._cash
        for symbol, pos in self._positions.items():
            price = await self._mark_price(symbol)
            equity += Decimal(str(pos["qty"])) * price
        return AccountSnapshot(
            broker=self.name, account_id="paper-1",
            equity=equity, cash=self._cash, buying_power=self._cash,
            day_pnl=self._realized_pnl_today,
            as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        result: list[Position] = []
        now = datetime.now(UTC)
        for symbol, pos in self._positions.items():
            qty = Decimal(str(pos["qty"]))
            avg = Decimal(str(pos["avg_price"]))
            price = await self._mark_price(symbol)
            result.append(
                Position(
                    symbol=symbol, quantity=qty, avg_entry_price=avg,
                    market_value=qty * price, unrealized_pnl=(price - avg) * qty,
                    broker=self.name, as_of=now,
                )
            )
        return result

    async def tax_lots(self, symbol: str | None = None) -> list[TaxLot]:
        return [
            TaxLot(
                symbol=lot["symbol"], quantity=Decimal(str(lot["qty"])),
                cost_basis=Decimal(str(lot["cost"])),
                acquired_at=datetime.fromisoformat(lot["acquired_at"]),
                broker=self.name,
            )
            for lot in self._lots
            if symbol is None or lot["symbol"] == symbol.upper()
        ]

    # -- orders --------------------------------------------------------------------

    async def submit_order(self, order: Order) -> Order:
        if any(o.client_order_id == order.client_order_id for o in self._open_orders.values()):
            raise OrderRejectedError(self.name, "duplicate client_order_id")
        order.broker = self.name
        order.broker_order_id = f"paper-{order.client_order_id[:12]}"
        order.status = OrderStatus.ACCEPTED
        order.updated_at = datetime.now(UTC)
        self._open_orders[order.id] = order
        await self._try_fill(order)
        self._save_state()
        return order

    async def cancel_order(self, order: Order) -> Order:
        tracked = self._open_orders.pop(order.id, None)
        if tracked is None or tracked.status.is_terminal:
            raise BrokerError(self.name, f"order {order.id} is not open", retryable=False)
        tracked.status = OrderStatus.CANCELED
        tracked.updated_at = datetime.now(UTC)
        self._save_state()
        return tracked

    async def order_status(self, order: Order) -> Order:
        tracked = self._open_orders.get(order.id, order)
        if not tracked.status.is_terminal:
            await self._try_fill(tracked)
        return tracked

    async def open_orders(self) -> list[Order]:
        for order in list(self._open_orders.values()):
            if not order.status.is_terminal:
                await self._try_fill(order)
        return [o for o in self._open_orders.values() if o.status.is_open_at_broker]

    async def recent_fills(self, *, limit: int = 50) -> list[Fill]:
        return self._fills[-limit:]

    # -- simulation internals ----------------------------------------------------------

    async def _mark_price(self, symbol: str) -> Decimal:
        quote = await self._require_quote(symbol)
        price = quote.mid or quote.last
        if price is None:
            raise BrokerError(self.name, f"no usable price for {symbol}")
        return price

    async def _require_quote(self, symbol: str) -> Quote:
        if self._quote_fn is None:
            raise BrokerError(
                self.name, "paper broker has no quote source wired — refusing to invent prices",
                retryable=False,
            )
        return await self._quote_fn(symbol)

    async def _try_fill(self, order: Order) -> None:
        quote = await self._require_quote(order.symbol)
        buy = order.side.is_buy
        book_price = (quote.ask if buy else quote.bid) or quote.last or quote.mid
        if book_price is None:
            return  # no price, no fill — try again on next poll
        if order.order_type.value == "limit" and order.limit_price is not None:
            marketable = book_price <= order.limit_price if buy else book_price >= order.limit_price
            if not marketable:
                return
            fill_price = order.limit_price if buy else max(book_price, order.limit_price)
            fill_price = min(book_price, order.limit_price) if buy else fill_price
        else:
            fill_price = book_price
        cost = order.quantity * fill_price
        if buy and cost > self._cash:
            order.status = OrderStatus.REJECTED_BROKER
            order.status_reason = "insufficient paper cash"
            order.updated_at = datetime.now(UTC)
            return
        self._apply_fill(order, fill_price)

    def _apply_fill(self, order: Order, price: Decimal) -> None:
        symbol = order.symbol.upper()
        qty = order.quantity
        now = datetime.now(UTC)
        pos = self._positions.get(symbol, {"qty": "0", "avg_price": "0"})
        cur_qty = Decimal(str(pos["qty"]))
        cur_avg = Decimal(str(pos["avg_price"]))
        if order.side.is_buy:
            new_qty = cur_qty + qty
            new_avg = ((cur_qty * cur_avg) + (qty * price)) / new_qty if new_qty else Decimal(0)
            self._cash -= qty * price
            self._positions[symbol] = {"qty": str(new_qty), "avg_price": str(new_avg)}
            self._lots.append(
                {"symbol": symbol, "qty": str(qty), "cost": str(price), "acquired_at": now.isoformat()}
            )
        else:
            if qty > cur_qty:
                order.status = OrderStatus.REJECTED_BROKER
                order.status_reason = "insufficient paper position (no shorting in paper broker)"
                order.updated_at = now
                return
            self._cash += qty * price
            self._realized_pnl_today += (price - cur_avg) * qty
            remaining = cur_qty - qty
            if remaining == 0:
                self._positions.pop(symbol, None)
            else:
                self._positions[symbol] = {"qty": str(remaining), "avg_price": str(cur_avg)}
            self._consume_lots(symbol, qty)
        order.status = OrderStatus.FILLED
        order.filled_quantity = qty
        order.avg_fill_price = price
        order.updated_at = now
        self._fills.append(
            Fill(order_id=order.id, broker_order_id=order.broker_order_id, symbol=symbol,
                 side=order.side, quantity=qty, price=price, filled_at=now, broker=self.name)
        )

    def _consume_lots(self, symbol: str, qty: Decimal) -> None:
        """FIFO lot consumption."""
        remaining = qty
        kept: list[dict[str, Any]] = []
        for lot in self._lots:
            if lot["symbol"] != symbol or remaining <= 0:
                kept.append(lot)
                continue
            lot_qty = Decimal(str(lot["qty"]))
            if lot_qty <= remaining:
                remaining -= lot_qty
            else:
                lot["qty"] = str(lot_qty - remaining)
                remaining = Decimal(0)
                kept.append(lot)
        self._lots = kept

    # -- persistence -----------------------------------------------------------------

    def _save_state(self) -> None:
        if self._state_file is None or self._read_only:
            return
        state = {
            "cash": str(self._cash),
            "positions": self._positions,
            "lots": self._lots,
            "realized_pnl_today": str(self._realized_pnl_today),
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)

    def _load_state(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        state = json.loads(self._state_file.read_text(encoding="utf-8"))
        self._cash = Decimal(state.get("cash", str(_DEFAULT_CASH)))
        self._positions = state.get("positions", {})
        self._lots = state.get("lots", [])
        self._realized_pnl_today = Decimal(state.get("realized_pnl_today", "0"))
