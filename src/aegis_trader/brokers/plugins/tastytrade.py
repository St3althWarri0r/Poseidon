"""tastytrade broker plugin (official Open API,
https://developer.tastytrade.com).

Credentials (vault JSON): {"username": "...", "password": "...",
"account_number": "..."} — or {"remember_token": "..."} after first login.
``paper: true`` targets the certification environment
(api.cert.tastyworks.com). Session tokens are obtained via POST /sessions
and sent in the Authorization header.
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
)
from ...core.errors import BrokerAuthError, BrokerError
from ...core.models import AccountSnapshot, Order, Position
from ..base import Broker

_LIVE = "https://api.tastyworks.com"
_CERT = "https://api.cert.tastyworks.com"

_STATUS_MAP = {
    "Received": OrderStatus.SUBMITTED,
    "Routed": OrderStatus.SUBMITTED,
    "In Flight": OrderStatus.SUBMITTED,
    "Live": OrderStatus.ACCEPTED,
    "Cancel Requested": OrderStatus.ACCEPTED,
    "Replace Requested": OrderStatus.ACCEPTED,
    "Contingent": OrderStatus.ACCEPTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELED,
    "Expired": OrderStatus.EXPIRED,
    "Rejected": OrderStatus.REJECTED_BROKER,
    "Removed": OrderStatus.CANCELED,
    "Partially Removed": OrderStatus.PARTIALLY_FILLED,
}

_ACTION_MAP = {
    OrderSide.BUY: "Buy to Open", OrderSide.SELL: "Sell to Close",
    OrderSide.BUY_TO_OPEN: "Buy to Open", OrderSide.BUY_TO_CLOSE: "Buy to Close",
    OrderSide.SELL_TO_OPEN: "Sell to Open", OrderSide.SELL_TO_CLOSE: "Sell to Close",
}


class TastytradeBroker(Broker):
    name = "tastytrade"
    display_name = "tastytrade"

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        super().__init__(credentials=credentials, paper=paper, timeout=timeout, options=options)
        self._base = _CERT if paper else _LIVE
        self._account_number = credentials.get("account_number", "")
        self._session_token: str | None = None

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset(
            {
                BrokerCapability.EQUITIES,
                BrokerCapability.OPTIONS,
                BrokerCapability.PAPER_TRADING,
                BrokerCapability.MARGIN,
            }
        )

    @property
    def _headers(self) -> dict[str, str]:
        if not self._session_token:
            raise BrokerAuthError(self.name, "not connected")
        return {"Authorization": self._session_token, "Accept": "application/json"}

    async def connect(self) -> None:
        body: dict[str, Any] = {"remember-me": True}
        if self._credentials.get("remember_token"):
            body["login"] = self._credentials.get("username", "")
            body["remember-token"] = self._credentials["remember_token"]
        else:
            try:
                body["login"] = self._credentials["username"]
                body["password"] = self._credentials["password"]
            except KeyError as exc:
                raise BrokerAuthError(self.name, f"credential missing field {exc}") from exc
        payload = await self._request("POST", f"{self._base}/sessions", json_body=body)
        data = (payload or {}).get("data") or {}
        token = data.get("session-token")
        if not token:
            raise BrokerAuthError(self.name, "no session token returned")
        self._session_token = token
        if not self._account_number:
            accounts = await self._get("/customers/me/accounts")
            items = ((accounts.get("data") or {}).get("items")) or []
            if not items:
                raise BrokerAuthError(self.name, "no accounts on this login")
            self._account_number = items[0]["account"]["account-number"]
        self._connected = True

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", f"{self._base}{path}", headers=self._headers, params=params)

    async def account(self) -> AccountSnapshot:
        payload = await self._get(f"/accounts/{self._account_number}/balances")
        b = (payload.get("data") or {})
        return AccountSnapshot(
            broker=self.name, account_id=self._account_number,
            equity=Decimal(str(b.get("net-liquidating-value", 0))),
            cash=Decimal(str(b.get("cash-balance", 0))),
            buying_power=Decimal(str(b.get("equity-buying-power", 0))),
            options_buying_power=Decimal(str(b["derivative-buying-power"])) if b.get("derivative-buying-power") is not None else None,
            maintenance_margin=Decimal(str(b["maintenance-requirement"])) if b.get("maintenance-requirement") is not None else None,
            as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        payload = await self._get(f"/accounts/{self._account_number}/positions")
        items = ((payload.get("data") or {}).get("items")) or []
        result: list[Position] = []
        now = datetime.now(UTC)
        for p in items:
            qty = Decimal(str(p.get("quantity", 0)))
            if p.get("quantity-direction") == "Short":
                qty = -qty
            instrument = p.get("instrument-type", "Equity")
            result.append(
                Position(
                    symbol=p.get("symbol", ""),
                    asset_class=AssetClass.OPTION if "Option" in instrument else AssetClass.EQUITY,
                    quantity=qty,
                    avg_entry_price=Decimal(str(p.get("average-open-price", 0))),
                    broker=self.name, as_of=now,
                )
            )
        return result

    async def submit_order(self, order: Order) -> Order:
        is_option = order.asset_class is AssetClass.OPTION
        leg = {
            "instrument-type": "Equity Option" if is_option else "Equity",
            "symbol": order.symbol,
            "quantity": int(order.quantity),
            "action": _ACTION_MAP[order.side],
        }
        body: dict[str, Any] = {
            "order-type": {
                OrderType.MARKET: "Market", OrderType.LIMIT: "Limit",
                OrderType.STOP: "Stop", OrderType.STOP_LIMIT: "Stop Limit",
            }.get(order.order_type, "Limit"),
            "time-in-force": {"day": "Day", "gtc": "GTC"}.get(order.time_in_force.value, "Day"),
            "legs": [leg],
        }
        if order.limit_price is not None:
            body["price"] = str(order.limit_price)
            body["price-effect"] = "Debit" if order.side.is_buy else "Credit"
        if order.stop_price is not None:
            body["stop-trigger"] = str(order.stop_price)
        payload = await self._request(
            "POST", f"{self._base}/accounts/{self._account_number}/orders",
            headers=self._headers, json_body=body,
        )
        data = ((payload or {}).get("data") or {}).get("order") or {}
        if not data.get("id"):
            raise BrokerError(self.name, f"order not accepted: {payload}", retryable=False)
        order.broker = self.name
        order.broker_order_id = str(data["id"])
        order.status = _STATUS_MAP.get(data.get("status", ""), OrderStatus.SUBMITTED)
        order.updated_at = datetime.now(UTC)
        return order

    async def cancel_order(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        await self._request(
            "DELETE", f"{self._base}/accounts/{self._account_number}/orders/{order.broker_order_id}",
            headers=self._headers,
        )
        order.status = OrderStatus.CANCELED
        order.updated_at = datetime.now(UTC)
        return order

    async def order_status(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        payload = await self._get(f"/accounts/{self._account_number}/orders/{order.broker_order_id}")
        row = (payload.get("data") or {})
        order.status = _STATUS_MAP.get(row.get("status", ""), order.status)
        order.updated_at = datetime.now(UTC)
        return order

    async def open_orders(self) -> list[Order]:
        payload = await self._get(f"/accounts/{self._account_number}/orders/live")
        items = ((payload.get("data") or {}).get("items")) or []
        orders: list[Order] = []
        for r in items:
            status = _STATUS_MAP.get(r.get("status", ""), OrderStatus.ACCEPTED)
            if status.is_terminal:
                continue
            legs = r.get("legs") or [{}]
            leg = legs[0]
            action = leg.get("action", "Buy to Open")
            side = {v: k for k, v in _ACTION_MAP.items()}.get(action, OrderSide.BUY)
            orders.append(
                Order(
                    client_order_id=f"tasty-{r.get('id')}",
                    broker=self.name, broker_order_id=str(r.get("id")),
                    symbol=leg.get("symbol", ""),
                    asset_class=AssetClass.OPTION if "Option" in leg.get("instrument-type", "") else AssetClass.EQUITY,
                    side=side,
                    order_type=OrderType.LIMIT if r.get("price") else OrderType.MARKET,
                    quantity=Decimal(str(leg.get("quantity", 1))),
                    limit_price=Decimal(str(r["price"])) if r.get("price") else None,
                    status=status,
                )
            )
        return orders
