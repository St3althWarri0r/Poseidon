"""Charles Schwab broker plugin (official Trader API for individuals,
https://developer.schwab.com).

Schwab's Trader API uses OAuth2 with a 30-minute access token and a
7-day refresh token. Poseidon stores both in the vault and refreshes the
access token automatically; when the *refresh* token expires, the user
must re-run the one-time authorization flow described in
docs/broker-setup.md (Schwab requires an interactive browser consent —
that step cannot be automated and Poseidon will not fake it).

Credentials (vault JSON):
  {"app_key": "...", "app_secret": "...", "refresh_token": "...",
   "account_hash": "..."}

``account_hash`` is the hashed account id from GET /accounts/accountNumbers;
``poseidon broker schwab-setup`` walks through obtaining all of these.
"""

from __future__ import annotations

import base64
import time
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

_API = "https://api.schwabapi.com"

_STATUS_MAP = {
    "AWAITING_PARENT_ORDER": OrderStatus.SUBMITTED,
    "AWAITING_CONDITION": OrderStatus.SUBMITTED,
    "AWAITING_STOP_CONDITION": OrderStatus.SUBMITTED,
    "AWAITING_MANUAL_REVIEW": OrderStatus.SUBMITTED,
    "ACCEPTED": OrderStatus.ACCEPTED,
    "PENDING_ACTIVATION": OrderStatus.ACCEPTED,
    "QUEUED": OrderStatus.SUBMITTED,
    "WORKING": OrderStatus.ACCEPTED,
    "REJECTED": OrderStatus.REJECTED_BROKER,
    "PENDING_CANCEL": OrderStatus.ACCEPTED,
    "CANCELED": OrderStatus.CANCELED,
    "PENDING_REPLACE": OrderStatus.ACCEPTED,
    "REPLACED": OrderStatus.CANCELED,
    "FILLED": OrderStatus.FILLED,
    "EXPIRED": OrderStatus.EXPIRED,
}

_INSTRUCTION_MAP = {
    OrderSide.BUY: "BUY", OrderSide.SELL: "SELL",
    OrderSide.BUY_TO_OPEN: "BUY_TO_OPEN", OrderSide.BUY_TO_CLOSE: "BUY_TO_CLOSE",
    OrderSide.SELL_TO_OPEN: "SELL_TO_OPEN", OrderSide.SELL_TO_CLOSE: "SELL_TO_CLOSE",
}


class SchwabBroker(Broker):
    name = "schwab"
    display_name = "Charles Schwab"

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        super().__init__(credentials=credentials, paper=False, timeout=timeout, options=options)
        # Schwab has no paper environment; `paper` is ignored (documented).
        try:
            self._app_key = credentials["app_key"]
            self._app_secret = credentials["app_secret"]
            self._refresh_token = credentials["refresh_token"]
            self._account_hash = credentials["account_hash"]
        except KeyError as exc:
            raise BrokerAuthError(self.name, f"credential missing field {exc}") from exc
        self._access_token: str | None = None
        self._token_expiry = 0.0

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset(
            {
                BrokerCapability.EQUITIES,
                BrokerCapability.OPTIONS,
                BrokerCapability.MARGIN,
                BrokerCapability.EXTENDED_HOURS,
            }
        )

    async def _ensure_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expiry - 60:
            return self._access_token
        basic = base64.b64encode(f"{self._app_key}:{self._app_secret}".encode()).decode()
        payload = await self._request(
            "POST", f"{_API}/v1/oauth/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
        )
        token = (payload or {}).get("access_token")
        if not token:
            raise BrokerAuthError(
                self.name,
                "token refresh failed — the 7-day refresh token has likely expired; "
                "re-run: poseidon broker schwab-setup",
            )
        self._access_token = str(token)
        self._token_expiry = time.monotonic() + float(payload.get("expires_in", 1800))
        return self._access_token

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._ensure_token()}", "Accept": "application/json"}

    async def connect(self) -> None:
        await self._ensure_token()
        await self.account()
        self._connected = True

    async def account(self) -> AccountSnapshot:
        payload = await self._request(
            "GET", f"{_API}/trader/v1/accounts/{self._account_hash}",
            headers=await self._auth_headers(),
        )
        acct = (payload or {}).get("securitiesAccount") or {}
        balances = acct.get("currentBalances") or {}
        return AccountSnapshot(
            broker=self.name, account_id=acct.get("accountNumber", self._account_hash[:8]),
            equity=Decimal(str(balances.get("liquidationValue", 0))),
            cash=Decimal(str(balances.get("cashBalance", 0))),
            buying_power=Decimal(str(balances.get("buyingPower", balances.get("cashAvailableForTrading", 0)))),
            maintenance_margin=Decimal(str(balances["maintenanceRequirement"])) if balances.get("maintenanceRequirement") is not None else None,
            as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        payload = await self._request(
            "GET", f"{_API}/trader/v1/accounts/{self._account_hash}",
            headers=await self._auth_headers(), params={"fields": "positions"},
        )
        acct = (payload or {}).get("securitiesAccount") or {}
        result: list[Position] = []
        now = datetime.now(UTC)
        for p in acct.get("positions", []) or []:
            instrument = p.get("instrument") or {}
            qty = Decimal(str(p.get("longQuantity", 0))) - Decimal(str(p.get("shortQuantity", 0)))
            asset_type = instrument.get("assetType", "EQUITY")
            result.append(
                Position(
                    symbol=instrument.get("symbol", ""),
                    asset_class=AssetClass.OPTION if asset_type == "OPTION" else AssetClass.EQUITY,
                    quantity=qty,
                    avg_entry_price=Decimal(str(p.get("averagePrice", 0))),
                    market_value=Decimal(str(p["marketValue"])) if p.get("marketValue") is not None else None,
                    unrealized_pnl=Decimal(str(p["longOpenProfitLoss"])) if p.get("longOpenProfitLoss") is not None else None,
                    broker=self.name, as_of=now,
                )
            )
        return result

    async def submit_order(self, order: Order) -> Order:
        is_option = order.asset_class is AssetClass.OPTION
        body: dict[str, Any] = {
            "orderType": {
                OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT",
                OrderType.STOP: "STOP", OrderType.STOP_LIMIT: "STOP_LIMIT",
                OrderType.TRAILING_STOP: "TRAILING_STOP",
            }.get(order.order_type, "LIMIT"),
            "session": "SEAMLESS" if order.extended_hours else "NORMAL",
            "duration": {"day": "DAY", "gtc": "GOOD_TILL_CANCEL"}.get(order.time_in_force.value, "DAY"),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": _INSTRUCTION_MAP[order.side],
                    "quantity": float(order.quantity),
                    "instrument": {
                        "symbol": order.symbol,
                        "assetType": "OPTION" if is_option else "EQUITY",
                    },
                }
            ],
        }
        if order.limit_price is not None:
            body["price"] = str(order.limit_price)
        if order.stop_price is not None:
            body["stopPrice"] = str(order.stop_price)
        # Schwab returns 201 with the order id in the Location header.
        headers = await self._auth_headers()
        try:
            response = await self._client.post(
                f"{_API}/trader/v1/accounts/{self._account_hash}/orders",
                headers=headers, json=body,
            )
        except Exception as exc:
            raise BrokerError(self.name, f"transport error: {exc}") from exc
        if response.status_code in (401, 403):
            raise BrokerAuthError(self.name)
        if response.status_code >= 400:
            raise BrokerError(self.name, f"order rejected HTTP {response.status_code}: {response.text[:300]}",
                              retryable=False)
        location = response.headers.get("Location", "")
        order.broker = self.name
        order.broker_order_id = location.rstrip("/").rsplit("/", 1)[-1] if location else None
        order.status = OrderStatus.SUBMITTED
        order.updated_at = datetime.now(UTC)
        return order

    async def cancel_order(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        await self._request(
            "DELETE",
            f"{_API}/trader/v1/accounts/{self._account_hash}/orders/{order.broker_order_id}",
            headers=await self._auth_headers(),
        )
        order.status = OrderStatus.CANCELED
        order.updated_at = datetime.now(UTC)
        return order

    async def order_status(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        row = await self._request(
            "GET",
            f"{_API}/trader/v1/accounts/{self._account_hash}/orders/{order.broker_order_id}",
            headers=await self._auth_headers(),
        )
        order.status = _STATUS_MAP.get((row or {}).get("status", ""), order.status)
        filled = (row or {}).get("filledQuantity")
        if filled is not None:
            order.filled_quantity = Decimal(str(filled))
        order.updated_at = datetime.now(UTC)
        return order

    async def open_orders(self) -> list[Order]:
        rows = await self._request(
            "GET", f"{_API}/trader/v1/accounts/{self._account_hash}/orders",
            headers=await self._auth_headers(),
            params={"maxResults": 100, "status": "WORKING"},
        )
        orders: list[Order] = []
        for r in rows or []:
            legs = r.get("orderLegCollection") or [{}]
            leg = legs[0]
            instruction = leg.get("instruction", "BUY")
            side = {v: k for k, v in _INSTRUCTION_MAP.items()}.get(instruction, OrderSide.BUY)
            instrument = leg.get("instrument") or {}
            orders.append(
                Order(
                    client_order_id=f"schwab-{r.get('orderId')}",
                    broker=self.name, broker_order_id=str(r.get("orderId")),
                    symbol=instrument.get("symbol", ""),
                    asset_class=AssetClass.OPTION if instrument.get("assetType") == "OPTION" else AssetClass.EQUITY,
                    side=side,
                    order_type=OrderType.LIMIT if r.get("price") else OrderType.MARKET,
                    quantity=Decimal(str(leg.get("quantity", 1))),
                    limit_price=Decimal(str(r["price"])) if r.get("price") else None,
                    time_in_force=TimeInForce.GTC if r.get("duration") == "GOOD_TILL_CANCEL" else TimeInForce.DAY,
                    status=_STATUS_MAP.get(r.get("status", ""), OrderStatus.ACCEPTED),
                )
            )
        return orders
