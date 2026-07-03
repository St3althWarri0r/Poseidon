"""Public.com broker plugin (official Trading API, https://public.com/api).

Implements the documented Public API contract (the same one Public's own
MCP server and ``publicdotcom-py`` SDK speak):

  * auth:   POST /userapiauthservice/personal/access-tokens
            {"validityInMinutes": N, "secret": ...} -> {"accessToken": ...}
            then ``Authorization: Bearer`` on every call
  * account:   GET  /userapigateway/trading/account
  * portfolio: GET  /userapigateway/trading/{accountId}/portfolio/v2
  * orders:    POST /userapigateway/trading/{accountId}/order
               POST /userapigateway/trading/{accountId}/order/multileg
               GET/DELETE /userapigateway/trading/{accountId}/order/{orderId}

Credentials (vault JSON): {"secret": "...", "account_id": "..."} —
``account_id`` optional (first account on the key is used). Generate the
secret in Public: Settings -> Security -> API. API access is free; normal
trading fees/rebates apply to executed orders.

Supported: stocks/ETFs (fractional), single-leg options, multi-leg option
strategies (via ``Order.legs``), crypto, extended-hours equities.
Public has **no paper environment** — the ``paper`` flag is rejected so a
misconfiguration cannot silently trade live money (use the built-in
``paper`` broker for simulation).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
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

_BASE = "https://api.public.com"
_TOKEN_VALIDITY_MINUTES = 1440  # refreshed well before expiry

_STATUS_MAP = {
    "NEW": OrderStatus.ACCEPTED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELED,
    "QUEUED_CANCELLED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED_BROKER,
    "PENDING_REPLACE": OrderStatus.ACCEPTED,
    "PENDING_CANCEL": OrderStatus.ACCEPTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "REPLACED": OrderStatus.CANCELED,
    "UNKNOWN": OrderStatus.SUBMITTED,
}

_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP: "STOP",
    OrderType.STOP_LIMIT: "STOP_LIMIT",
}

# Public splits direction into side + open/close indicator for options.
_SIDE_MAP: dict[OrderSide, tuple[str, str | None]] = {
    OrderSide.BUY: ("BUY", None),
    OrderSide.SELL: ("SELL", None),
    OrderSide.BUY_TO_OPEN: ("BUY", "OPEN"),
    OrderSide.BUY_TO_CLOSE: ("BUY", "CLOSE"),
    OrderSide.SELL_TO_OPEN: ("SELL", "OPEN"),
    OrderSide.SELL_TO_CLOSE: ("SELL", "CLOSE"),
}

_INSTRUMENT_TYPE_MAP = {
    AssetClass.EQUITY: "EQUITY",
    AssetClass.ETF: "EQUITY",
    AssetClass.OPTION: "OPTION",
    AssetClass.CRYPTO: "CRYPTO",
}


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _client_uuid(client_order_id: str) -> str:
    """Public requires the client-supplied orderId to be an RFC-4122 UUID.

    Our client_order_id is a uuid4 hex string; render it canonically. A
    non-UUID id (imported/legacy) gets a deterministic UUID5 so retries of
    the same logical order still collide server-side (idempotency intact).
    """
    try:
        return str(uuid.UUID(client_order_id))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"poseidon:{client_order_id}"))


class PublicBroker(Broker):
    name = "public"
    display_name = "Public.com"

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        super().__init__(credentials=credentials, paper=False, timeout=timeout, options=options)
        if paper:
            raise BrokerError(
                self.name,
                "Public.com has no paper environment — set `paper: false` explicitly to "
                "acknowledge live trading, or use the built-in `paper` broker for simulation",
                retryable=False,
            )
        try:
            self._secret = credentials["secret"]
        except KeyError as exc:
            raise BrokerAuthError(self.name, f"credential missing field {exc}") from exc
        self._account_id = credentials.get("account_id", "")
        self._access_token: str | None = None
        self._token_expiry = 0.0

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset(
            {
                BrokerCapability.EQUITIES,
                BrokerCapability.OPTIONS,
                BrokerCapability.CRYPTO,
                BrokerCapability.FRACTIONAL_SHARES,
                BrokerCapability.EXTENDED_HOURS,
                BrokerCapability.MARGIN,
            }
        )

    # -- auth ----------------------------------------------------------------

    async def _ensure_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expiry - 300:
            return self._access_token
        payload = await self._request(
            "POST", f"{_BASE}/userapiauthservice/personal/access-tokens",
            json_body={"validityInMinutes": _TOKEN_VALIDITY_MINUTES, "secret": self._secret},
        )
        token = (payload or {}).get("accessToken")
        if not token:
            raise BrokerAuthError(self.name, "no accessToken in auth response")
        self._access_token = str(token)
        self._token_expiry = time.monotonic() + _TOKEN_VALIDITY_MINUTES * 60
        return self._access_token

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._ensure_token()}"}

    async def connect(self) -> None:
        headers = await self._auth_headers()
        payload = await self._request("GET", f"{_BASE}/userapigateway/trading/account",
                                      headers=headers)
        accounts = (payload or {}).get("accounts") or []
        if not accounts:
            raise BrokerAuthError(self.name, "no accounts available on this API key")
        if self._account_id:
            if not any(a.get("accountId") == self._account_id for a in accounts):
                raise BrokerAuthError(self.name, f"account {self._account_id} not on this key")
        else:
            self._account_id = accounts[0]["accountId"]
        self._connected = True

    # -- portfolio ------------------------------------------------------------

    async def _portfolio(self) -> dict[str, Any]:
        return await self._request(
            "GET", f"{_BASE}/userapigateway/trading/{self._account_id}/portfolio/v2",
            headers=await self._auth_headers(),
        ) or {}

    async def account(self) -> AccountSnapshot:
        portfolio = await self._portfolio()
        buying_power = portfolio.get("buyingPower") or {}
        equity_total = Decimal(0)
        cash = Decimal(0)
        for bucket in portfolio.get("equity") or []:
            value = _dec(bucket.get("value")) or Decimal(0)
            equity_total += value
            if bucket.get("type") == "CASH":
                cash += value
        return AccountSnapshot(
            broker=self.name,
            account_id=self._account_id,
            equity=equity_total,
            cash=cash,
            buying_power=_dec(buying_power.get("buyingPower")) or Decimal(0),
            options_buying_power=_dec(buying_power.get("optionsBuyingPower")),
            as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        portfolio = await self._portfolio()
        now = datetime.now(UTC)
        result: list[Position] = []
        for p in portfolio.get("positions") or []:
            instrument = p.get("instrument") or {}
            qty = _dec(p.get("quantity")) or Decimal(0)
            if qty == 0:
                continue
            cost_basis = p.get("costBasis") or {}
            instrument_type = instrument.get("type", "EQUITY")
            asset_class = {
                "OPTION": AssetClass.OPTION,
                "CRYPTO": AssetClass.CRYPTO,
            }.get(instrument_type, AssetClass.EQUITY)
            result.append(
                Position(
                    symbol=instrument.get("symbol", ""),
                    asset_class=asset_class,
                    quantity=qty,
                    avg_entry_price=_dec(cost_basis.get("unitCost")) or Decimal(0),
                    market_value=_dec(p.get("currentValue")),
                    unrealized_pnl=_dec(cost_basis.get("gainValue")),
                    broker=self.name,
                    as_of=now,
                )
            )
        return result

    # -- orders ----------------------------------------------------------------

    def _expiration_block(self, order: Order) -> dict[str, Any]:
        if order.time_in_force is TimeInForce.GTC:
            # Public supports DAY and GTD; GTC maps to a 30-day GTD window
            # (documented in docs/broker-setup.md).
            until = datetime.now(UTC) + timedelta(days=30)
            return {"timeInForce": "GTD", "expirationTime": until.isoformat()}
        return {"timeInForce": "DAY"}

    def _single_leg_payload(self, order: Order) -> dict[str, Any]:
        side, open_close = _SIDE_MAP[order.side]
        order_type = _ORDER_TYPE_MAP.get(order.order_type)
        if order_type is None:
            raise BrokerError(self.name, f"unsupported order type {order.order_type}",
                              retryable=False)
        payload: dict[str, Any] = {
            "orderId": _client_uuid(order.client_order_id),
            "instrument": {
                "symbol": order.symbol,
                "type": _INSTRUMENT_TYPE_MAP.get(order.asset_class, "EQUITY"),
            },
            "orderSide": side,
            "orderType": order_type,
            "expiration": self._expiration_block(order),
            "quantity": str(order.quantity),
        }
        if open_close:
            payload["openCloseIndicator"] = open_close
        if order.limit_price is not None:
            payload["limitPrice"] = str(order.limit_price)
        if order.stop_price is not None:
            payload["stopPrice"] = str(order.stop_price)
        if order.extended_hours and order.asset_class in (AssetClass.EQUITY, AssetClass.ETF):
            payload["equityMarketSession"] = "EXTENDED"
        return payload

    def _multileg_payload(self, order: Order) -> dict[str, Any]:
        if order.order_type is not OrderType.LIMIT or order.limit_price is None:
            raise BrokerError(self.name, "multi-leg orders must be LIMIT with a net price",
                              retryable=False)
        legs = []
        for leg in order.legs:
            side, open_close = _SIDE_MAP[leg.side]
            leg_payload: dict[str, Any] = {
                "instrument": {"symbol": leg.contract_symbol, "type": "OPTION"},
                "side": side,
                "ratioQuantity": leg.quantity,
            }
            if open_close:
                leg_payload["openCloseIndicator"] = open_close
            legs.append(leg_payload)
        return {
            "orderId": _client_uuid(order.client_order_id),
            "quantity": int(order.quantity),
            "type": "LIMIT",
            "limitPrice": str(order.limit_price),
            "expiration": self._expiration_block(order),
            "legs": legs,
        }

    async def preflight(self, order: Order) -> str | None:
        """Public's preflight endpoints validate the order against live
        account state (buying power, margin, short locate) without placing
        it. A definitive rejection (HTTP 4xx with a reason) is returned as
        the reason string; transport/server errors return None so a flaky
        preflight can never veto an order that submit would accept."""
        try:
            if order.legs:
                payload = self._multileg_payload(order)
                payload.pop("orderId", None)
                payload["orderType"] = payload.pop("type", "LIMIT")
                path = f"{_BASE}/userapigateway/trading/{self._account_id}/preflight/multi-leg"
            else:
                payload = self._single_leg_payload(order)
                payload.pop("orderId", None)
                path = f"{_BASE}/userapigateway/trading/{self._account_id}/preflight/single-leg"
            await self._request("POST", path, headers=await self._auth_headers(),
                                json_body=payload)
        except BrokerError as exc:
            if exc.retryable:  # 5xx/timeout: preflight unavailable, not a verdict
                return None
            return f"broker preflight rejected the order: {exc}"
        return None

    async def submit_order(self, order: Order) -> Order:
        headers = await self._auth_headers()
        if order.legs:
            path = f"{_BASE}/userapigateway/trading/{self._account_id}/order/multileg"
            payload = self._multileg_payload(order)
        else:
            path = f"{_BASE}/userapigateway/trading/{self._account_id}/order"
            payload = self._single_leg_payload(order)
        response = await self._request("POST", path, headers=headers, json_body=payload)
        broker_order_id = (response or {}).get("orderId")
        if not broker_order_id:
            raise BrokerError(self.name, f"order not accepted: {response}", retryable=False)
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
            f"{_BASE}/userapigateway/trading/{self._account_id}/order/{order.broker_order_id}",
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
            f"{_BASE}/userapigateway/trading/{self._account_id}/order/{order.broker_order_id}",
            headers=await self._auth_headers(),
        ) or {}
        order.status = _STATUS_MAP.get(row.get("status", ""), order.status)
        if row.get("filledQuantity") is not None:
            order.filled_quantity = _dec(row["filledQuantity"]) or Decimal(0)
        if row.get("averagePrice") is not None:
            order.avg_fill_price = _dec(row["averagePrice"])
        if row.get("rejectReason"):
            order.status_reason = str(row["rejectReason"])
        order.updated_at = datetime.now(UTC)
        return order

    async def open_orders(self) -> list[Order]:
        # portfolio/v2 carries the account's working orders.
        portfolio = await self._portfolio()
        orders: list[Order] = []
        for row in portfolio.get("orders") or []:
            status = _STATUS_MAP.get(row.get("status", ""), OrderStatus.ACCEPTED)
            if status.is_terminal:
                continue
            instrument = row.get("instrument") or {}
            side = OrderSide.BUY if row.get("side") == "BUY" else OrderSide.SELL
            open_close = row.get("openCloseIndicator")
            if instrument.get("type") == "OPTION" and open_close:
                side = {
                    ("BUY", "OPEN"): OrderSide.BUY_TO_OPEN,
                    ("BUY", "CLOSE"): OrderSide.BUY_TO_CLOSE,
                    ("SELL", "OPEN"): OrderSide.SELL_TO_OPEN,
                    ("SELL", "CLOSE"): OrderSide.SELL_TO_CLOSE,
                }.get((row.get("side", "BUY"), open_close), side)
            expiration = row.get("expiration") or {}
            quantity = _dec(row.get("quantity")) or _dec(row.get("notionalValue")) or Decimal("1")
            orders.append(
                Order(
                    client_order_id=str(row.get("orderId", "")),
                    broker=self.name,
                    broker_order_id=str(row.get("orderId", "")),
                    symbol=instrument.get("symbol", ""),
                    asset_class={
                        "OPTION": AssetClass.OPTION, "CRYPTO": AssetClass.CRYPTO,
                    }.get(instrument.get("type", ""), AssetClass.EQUITY),
                    side=side,
                    order_type={
                        "MARKET": OrderType.MARKET, "LIMIT": OrderType.LIMIT,
                        "STOP": OrderType.STOP, "STOP_LIMIT": OrderType.STOP_LIMIT,
                    }.get(row.get("type", "LIMIT"), OrderType.LIMIT),
                    quantity=quantity,
                    limit_price=_dec(row.get("limitPrice")),
                    stop_price=_dec(row.get("stopPrice")),
                    time_in_force=TimeInForce.GTC
                    if expiration.get("timeInForce") == "GTD" else TimeInForce.DAY,
                    status=status,
                )
            )
        return orders
