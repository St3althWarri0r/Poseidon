"""Tradier broker plugin (official Brokerage API,
https://documentation.tradier.com/brokerage-api).

Credentials (vault JSON): {"access_token": "...", "account_id": "..."}.
``paper: true`` targets the sandbox host. Supports equities and single-leg
options with a Bearer token; form-encoded order tickets with a ``tag``
field used as the client order ID.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ...core.enums import (
    AssetClass,
    BrokerCapability,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from ...core.errors import BrokerAuthError, BrokerError
from ...core.models import AccountSnapshot, Order, Position
from ..base import Broker

_LIVE = "https://api.tradier.com/v1"
_SANDBOX = "https://sandbox.tradier.com/v1"

_STATUS_MAP = {
    "pending": OrderStatus.SUBMITTED,
    "open": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED_BROKER,
    "error": OrderStatus.ERROR,
}

_EQUITY_SIDES = {
    OrderSide.BUY: "buy", OrderSide.SELL: "sell",
}
_OPTION_SIDES = {
    OrderSide.BUY_TO_OPEN: "buy_to_open", OrderSide.BUY_TO_CLOSE: "buy_to_close",
    OrderSide.SELL_TO_OPEN: "sell_to_open", OrderSide.SELL_TO_CLOSE: "sell_to_close",
}


def _as_list(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


class TradierBroker(Broker):
    name = "tradier"
    display_name = "Tradier"

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        super().__init__(credentials=credentials, paper=paper, timeout=timeout, options=options)
        try:
            token = credentials["access_token"]
            self._account_id = credentials["account_id"]
        except KeyError as exc:
            raise BrokerAuthError(self.name, f"credential missing field {exc}") from exc
        self._base = _SANDBOX if paper else _LIVE
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset(
            {
                BrokerCapability.EQUITIES,
                BrokerCapability.OPTIONS,
                BrokerCapability.PAPER_TRADING,
                BrokerCapability.MARGIN,
            }
        )

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", f"{self._base}{path}", headers=self._headers, params=params)

    async def connect(self) -> None:
        profile = await self._get("/user/profile")
        accounts = _as_list((profile.get("profile") or {}).get("account"))
        if not any(a.get("account_number") == self._account_id for a in accounts):
            raise BrokerAuthError(self.name, f"account {self._account_id} not found on this token")
        self._connected = True

    async def account(self) -> AccountSnapshot:
        payload = await self._get(f"/accounts/{self._account_id}/balances")
        b = payload.get("balances") or {}
        margin = b.get("margin") or {}
        cash_block = b.get("cash") or {}
        buying_power = margin.get("stock_buying_power") or cash_block.get("cash_available") or b.get("total_cash", 0)
        return AccountSnapshot(
            broker=self.name, account_id=self._account_id,
            equity=Decimal(str(b.get("total_equity", 0))),
            cash=Decimal(str(b.get("total_cash", 0))),
            buying_power=Decimal(str(buying_power or 0)),
            options_buying_power=Decimal(str(margin["option_buying_power"])) if margin.get("option_buying_power") is not None else None,
            day_pnl=Decimal(str(b["close_pl"])) if b.get("close_pl") is not None else None,
            as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        payload = await self._get(f"/accounts/{self._account_id}/positions")
        rows = _as_list((payload.get("positions") or {}).get("position") if isinstance(payload.get("positions"), dict) else None)
        result: list[Position] = []
        now = datetime.now(UTC)
        for p in rows:
            qty = Decimal(str(p.get("quantity", 0)))
            cost = Decimal(str(p.get("cost_basis", 0)))
            symbol = p.get("symbol", "")
            asset_class = AssetClass.OPTION if len(symbol) > 12 else AssetClass.EQUITY
            result.append(
                Position(
                    symbol=symbol, asset_class=asset_class, quantity=qty,
                    avg_entry_price=(cost / qty) if qty else Decimal(0),
                    broker=self.name, as_of=now,
                )
            )
        return result

    async def submit_order(self, order: Order) -> Order:
        if order.quantity != order.quantity.to_integral_value():
            raise BrokerError(self.name, "fractional share quantities are not supported",
                              retryable=False)
        is_option = order.asset_class is AssetClass.OPTION
        data: dict[str, Any] = {
            "class": "option" if is_option else "equity",
            "symbol": order.symbol.upper() if not is_option else "",
            "duration": {"day": "day", "gtc": "gtc"}.get(order.time_in_force.value, "day"),
            "type": {
                OrderType.MARKET: "market", OrderType.LIMIT: "limit",
                OrderType.STOP: "stop", OrderType.STOP_LIMIT: "stop_limit",
            }.get(order.order_type, "limit"),
            "quantity": str(int(order.quantity)),
            "tag": order.client_order_id[:36],
        }
        if is_option:
            if order.side not in _OPTION_SIDES:
                raise BrokerError(self.name, f"invalid option side {order.side}", retryable=False)
            # Tradier wants the underlying in `symbol` and the OCC in `option_symbol`.
            occ = order.symbol.upper()
            root = occ[: next(i for i, c in enumerate(occ) if c.isdigit())]
            data["symbol"] = root
            data["option_symbol"] = occ
            data["side"] = _OPTION_SIDES[order.side]
        else:
            if order.side not in _EQUITY_SIDES:
                raise BrokerError(self.name, f"invalid equity side {order.side}", retryable=False)
            data["side"] = _EQUITY_SIDES[order.side]
        if order.limit_price is not None:
            data["price"] = str(order.limit_price)
        if order.stop_price is not None:
            data["stop"] = str(order.stop_price)
        # Tradier's 'tag' is a label, not an enforced idempotency key, so a
        # submit timeout has an unknown outcome and must not be auto-retried.
        payload = await self._request(
            "POST", f"{self._base}/accounts/{self._account_id}/orders",
            headers=self._headers, data=data, idempotent=False,
        )
        result = (payload or {}).get("order") or {}
        if not result.get("id"):
            # A successful Tradier order response always carries order.id.
            # Tradier can answer HTTP 200 with an `errors` element and no
            # `order` key — a definitive rejection. Any other id-less 2xx has
            # an UNKNOWN outcome: raise ambiguous so the manager marks it
            # ERROR and reconciles it against the broker at startup, instead
            # of persisting a phantom SUBMITTED order with an empty
            # broker_order_id that can never be polled or canceled.
            if (payload or {}).get("errors"):
                raise BrokerError(self.name, f"order rejected: {payload}", retryable=False)
            raise BrokerError(self.name, f"no order id in submit response: {payload!r}",
                              retryable=False, ambiguous=True)
        order.broker = self.name
        order.broker_order_id = str(result.get("id", ""))
        order.status = OrderStatus.SUBMITTED
        order.updated_at = datetime.now(UTC)
        return order

    async def cancel_order(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        await self._request(
            "DELETE", f"{self._base}/accounts/{self._account_id}/orders/{order.broker_order_id}",
            headers=self._headers,
        )
        # Cancel is asynchronous at the broker: the DELETE only queues the
        # request and in-flight fills can still occur. Adopt the broker's
        # authoritative state (pending-cancel maps to a non-terminal status)
        # so the lifecycle poller carries the order to its true terminal
        # state with any last-moment fills attached.
        try:
            return await self.order_status(order)
        except BrokerError:
            order.status = OrderStatus.ACCEPTED
            order.status_reason = "cancel requested — awaiting broker confirmation"
            order.updated_at = datetime.now(UTC)
            return order

    async def order_status(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        payload = await self._get(f"/accounts/{self._account_id}/orders/{order.broker_order_id}")
        row = payload.get("order") or {}
        order.status = _STATUS_MAP.get(row.get("status", ""), order.status)
        if row.get("exec_quantity"):
            order.filled_quantity = Decimal(str(row["exec_quantity"]))
        if row.get("avg_fill_price"):
            order.avg_fill_price = Decimal(str(row["avg_fill_price"]))
        order.updated_at = datetime.now(UTC)
        return order

    async def open_orders(self) -> list[Order]:
        payload = await self._get(f"/accounts/{self._account_id}/orders")
        node = payload.get("orders")
        rows = _as_list(node.get("order")) if isinstance(node, dict) else []
        orders: list[Order] = []
        for r in rows:
            status = _STATUS_MAP.get(r.get("status", ""), OrderStatus.ACCEPTED)
            if status.is_terminal:
                continue
            side_raw = r.get("side", "buy")
            side = (
                _OPTION_SIDES_INV.get(side_raw)
                or (OrderSide.BUY if side_raw == "buy" else OrderSide.SELL)
            )
            orders.append(
                Order(
                    client_order_id=r.get("tag") or f"tradier-{r.get('id')}",
                    broker=self.name, broker_order_id=str(r.get("id")),
                    symbol=r.get("option_symbol") or r.get("symbol", ""),
                    asset_class=AssetClass.OPTION if r.get("option_symbol") else AssetClass.EQUITY,
                    side=side,
                    order_type={"market": OrderType.MARKET, "limit": OrderType.LIMIT,
                                "stop": OrderType.STOP, "stop_limit": OrderType.STOP_LIMIT}
                               .get(r.get("type", "limit"), OrderType.LIMIT),
                    quantity=Decimal(str(r.get("quantity", 1))),
                    limit_price=Decimal(str(r["price"])) if r.get("price") else None,
                    time_in_force=TimeInForce.GTC if r.get("duration") == "gtc" else TimeInForce.DAY,
                    status=status,
                )
            )
        return orders


_OPTION_SIDES_INV = {v: k for k, v in _OPTION_SIDES.items()}
