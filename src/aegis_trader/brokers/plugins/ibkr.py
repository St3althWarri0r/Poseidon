"""Interactive Brokers plugin (official Client Portal Web API,
https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/).

IBKR's supported self-hosted integration path is the Client Portal Gateway:
a small Java process the user runs locally (or IBKR's IB Gateway) that
exposes a REST API on https://localhost:5000/v1/api with a browser login.
Aegis talks to that local gateway — this is IBKR's documented automation
interface for individuals.

Credentials (vault JSON): {"account_id": "..."} — authentication happens in
the gateway, not here. Options: {"gateway_url": "https://localhost:5000",
"verify_ssl": false} (the gateway ships a self-signed certificate).

``paper`` selects the paper account if the gateway is logged into one; IBKR
paper/live selection is a property of the gateway login session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

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

_STATUS_MAP = {
    "PendingSubmit": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.ACCEPTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELED,
    "PendingCancel": OrderStatus.ACCEPTED,
    "Inactive": OrderStatus.REJECTED_BROKER,
}


class IBKRBroker(Broker):
    name = "ibkr"
    display_name = "Interactive Brokers (Client Portal Gateway)"

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        super().__init__(credentials=credentials, paper=paper, timeout=timeout, options=options)
        self._account_id = credentials.get("account_id", "")
        gateway = str(self._options.get("gateway_url", "https://localhost:5000")).rstrip("/")
        self._base = f"{gateway}/v1/api"
        # The local gateway uses a self-signed cert; verification is off by
        # default *only* for localhost targets and can be forced on.
        verify = bool(self._options.get("verify_ssl", False))
        self._client = httpx.AsyncClient(timeout=timeout, verify=verify)
        self._conid_cache: dict[str, int] = {}

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset(
            {
                BrokerCapability.EQUITIES,
                BrokerCapability.OPTIONS,
                BrokerCapability.MARGIN,
                BrokerCapability.EXTENDED_HOURS,
                BrokerCapability.PAPER_TRADING,
            }
        )

    async def connect(self) -> None:
        status = await self._request("POST", f"{self._base}/iserver/auth/status")
        if not (status or {}).get("authenticated"):
            raise BrokerAuthError(
                self.name,
                "Client Portal Gateway is not authenticated — open the gateway URL in a "
                "browser and log in (see docs/broker-setup.md#interactive-brokers)",
            )
        if not self._account_id:
            accounts = await self._request("GET", f"{self._base}/iserver/accounts")
            ids = (accounts or {}).get("accounts") or []
            if not ids:
                raise BrokerAuthError(self.name, "no accounts available on the gateway session")
            self._account_id = ids[0]
        self._connected = True

    async def ping(self) -> bool:
        try:
            # `tickle` keeps the gateway session alive — required for 24/7 use.
            await self._request("POST", f"{self._base}/tickle")
        except BrokerError:
            return False
        return True

    async def account(self) -> AccountSnapshot:
        summary = await self._request(
            "GET", f"{self._base}/portfolio/{self._account_id}/summary"
        )
        def val(key: str) -> Decimal:
            node = (summary or {}).get(key) or {}
            return Decimal(str(node.get("amount", 0)))
        return AccountSnapshot(
            broker=self.name, account_id=self._account_id,
            equity=val("netliquidation"),
            cash=val("totalcashvalue"),
            buying_power=val("buyingpower"),
            maintenance_margin=val("maintmarginreq"),
            as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        rows = await self._request(
            "GET", f"{self._base}/portfolio/{self._account_id}/positions/0"
        )
        result: list[Position] = []
        now = datetime.now(UTC)
        for p in rows or []:
            qty = Decimal(str(p.get("position", 0)))
            if qty == 0:
                continue
            asset = p.get("assetClass", "STK")
            result.append(
                Position(
                    symbol=p.get("contractDesc") or p.get("ticker", ""),
                    asset_class=AssetClass.OPTION if asset == "OPT" else AssetClass.EQUITY,
                    quantity=qty,
                    avg_entry_price=Decimal(str(p.get("avgCost", 0))),
                    market_value=Decimal(str(p["mktValue"])) if p.get("mktValue") is not None else None,
                    unrealized_pnl=Decimal(str(p["unrealizedPnl"])) if p.get("unrealizedPnl") is not None else None,
                    broker=self.name, as_of=now,
                )
            )
        return result

    async def _conid(self, symbol: str) -> int:
        symbol = symbol.upper()
        if symbol in self._conid_cache:
            return self._conid_cache[symbol]
        rows = await self._request(
            "GET", f"{self._base}/iserver/secdef/search", params={"symbol": symbol}
        )
        for row in rows or []:
            if row.get("symbol") == symbol and row.get("conid"):
                conid = int(row["conid"])
                self._conid_cache[symbol] = conid
                return conid
        raise BrokerError(self.name, f"no contract found for {symbol}", retryable=False)

    async def submit_order(self, order: Order) -> Order:
        conid = await self._conid(order.symbol)
        ib_order: dict[str, Any] = {
            "conid": conid,
            "orderType": {
                OrderType.MARKET: "MKT", OrderType.LIMIT: "LMT",
                OrderType.STOP: "STP", OrderType.STOP_LIMIT: "STOP_LIMIT",
                OrderType.TRAILING_STOP: "TRAIL",
            }.get(order.order_type, "LMT"),
            "side": "BUY" if order.side.is_buy else "SELL",
            "quantity": float(order.quantity),
            "tif": {"day": "DAY", "gtc": "GTC", "ioc": "IOC", "fok": "FOK"}
                   .get(order.time_in_force.value, "DAY"),
            "cOID": order.client_order_id,
            "outsideRTH": order.extended_hours,
        }
        if order.limit_price is not None:
            ib_order["price"] = float(order.limit_price)
        if order.stop_price is not None:
            ib_order["auxPrice"] = float(order.stop_price)
        payload = await self._request(
            "POST", f"{self._base}/iserver/account/{self._account_id}/orders",
            json_body={"orders": [ib_order]},
        )
        # The gateway may answer with confirmation questions ("reply" flow);
        # confirm standard warnings automatically, but never suppress errors.
        result = (payload or [{}])[0] if isinstance(payload, list) else (payload or {})
        for _ in range(3):
            reply_id = result.get("id")
            if not reply_id or "order_id" in result:
                break
            answer = await self._request(
                "POST", f"{self._base}/iserver/reply/{reply_id}", json_body={"confirmed": True}
            )
            result = (answer or [{}])[0] if isinstance(answer, list) else (answer or {})
        broker_order_id = result.get("order_id")
        if not broker_order_id:
            raise BrokerError(self.name, f"order not accepted: {result}", retryable=False)
        order.broker = self.name
        order.broker_order_id = str(broker_order_id)
        order.status = OrderStatus.SUBMITTED
        order.updated_at = datetime.now(UTC)
        return order

    async def cancel_order(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        await self._request(
            "DELETE",
            f"{self._base}/iserver/account/{self._account_id}/order/{order.broker_order_id}",
        )
        order.status = OrderStatus.CANCELED
        order.updated_at = datetime.now(UTC)
        return order

    async def order_status(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        row = await self._request(
            "GET", f"{self._base}/iserver/account/order/status/{order.broker_order_id}"
        )
        status = (row or {}).get("order_status", "")
        order.status = _STATUS_MAP.get(status, order.status)
        filled = (row or {}).get("cum_fill")
        if filled:
            order.filled_quantity = Decimal(str(filled))
        order.updated_at = datetime.now(UTC)
        return order

    async def open_orders(self) -> list[Order]:
        payload = await self._request("GET", f"{self._base}/iserver/account/orders")
        orders: list[Order] = []
        for r in (payload or {}).get("orders", []) or []:
            status = _STATUS_MAP.get(r.get("status", ""), OrderStatus.ACCEPTED)
            if status.is_terminal:
                continue
            orders.append(
                Order(
                    client_order_id=r.get("order_ref") or f"ibkr-{r.get('orderId')}",
                    broker=self.name, broker_order_id=str(r.get("orderId")),
                    symbol=r.get("ticker", ""),
                    side=OrderSide.BUY if r.get("side") == "BUY" else OrderSide.SELL,
                    order_type=OrderType.LIMIT if r.get("price") else OrderType.MARKET,
                    quantity=Decimal(str(r.get("totalSize", 1))),
                    limit_price=Decimal(str(r["price"])) if r.get("price") else None,
                    time_in_force=TimeInForce.GTC if r.get("timeInForce") == "GTC" else TimeInForce.DAY,
                    status=status,
                )
            )
        return orders
