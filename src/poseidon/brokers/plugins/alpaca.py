"""Alpaca broker plugin (official Trading API, https://docs.alpaca.markets).

Credentials (vault JSON): {"key_id": "...", "secret_key": "..."}.
``paper: true`` targets https://paper-api.alpaca.markets.

Supports equities (incl. fractional), options (level permitting), extended
hours, client order IDs (idempotency), and streaming account activity via
polling (websocket streaming is handled by the sync service's poll cadence).
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
from ...core.models import AccountSnapshot, Fill, Order, Position
from ..base import Broker

_LIVE = "https://api.alpaca.markets"
_PAPER = "https://paper-api.alpaca.markets"

_STATUS_MAP = {
    "new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.EXPIRED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "replaced": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.ACCEPTED,
    "pending_replace": OrderStatus.ACCEPTED,
    "rejected": OrderStatus.REJECTED_BROKER,
    "suspended": OrderStatus.REJECTED_BROKER,
    "stopped": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
}

_SIDE_MAP = {
    OrderSide.BUY: "buy", OrderSide.SELL: "sell",
    OrderSide.BUY_TO_OPEN: "buy", OrderSide.BUY_TO_CLOSE: "buy",
    OrderSide.SELL_TO_OPEN: "sell", OrderSide.SELL_TO_CLOSE: "sell",
}

# Alpaca crypto accepts only these time-in-force values; the equity TIFs
# (day/opg/cls) are rejected with HTTP 422 42210000 "invalid crypto
# time_in_force". A valid crypto TIF is preserved; the rest are remapped to gtc.
_CRYPTO_TIF = frozenset({TimeInForce.GTC, TimeInForce.IOC, TimeInForce.FOK})


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AlpacaBroker(Broker):
    name = "alpaca"
    display_name = "Alpaca"

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        super().__init__(credentials=credentials, paper=paper, timeout=timeout, options=options)
        try:
            self._headers = {
                "APCA-API-KEY-ID": credentials["key_id"],
                "APCA-API-SECRET-KEY": credentials["secret_key"],
            }
        except KeyError as exc:
            raise BrokerAuthError(self.name, f"credential missing field {exc}") from exc
        self._base = _PAPER if paper else _LIVE

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset(
            {
                BrokerCapability.EQUITIES,
                BrokerCapability.OPTIONS,
                BrokerCapability.CRYPTO,
                BrokerCapability.FRACTIONAL_SHARES,
                BrokerCapability.EXTENDED_HOURS,
                BrokerCapability.PAPER_TRADING,
                BrokerCapability.STREAMING,
                BrokerCapability.MARGIN,
            }
        )

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", f"{self._base}{path}", headers=self._headers, params=params)

    async def connect(self) -> None:
        account = await self._get("/v2/account")
        if account.get("status") not in ("ACTIVE", "PAPER_ONLY"):
            raise BrokerAuthError(self.name, f"account status is {account.get('status')}")
        self._connected = True

    async def account(self) -> AccountSnapshot:
        a = await self._get("/v2/account")
        equity = Decimal(a["equity"])
        last_equity = Decimal(a.get("last_equity", a["equity"]))
        return AccountSnapshot(
            broker=self.name, account_id=a["account_number"],
            equity=equity,
            cash=Decimal(a["cash"]),
            buying_power=Decimal(a["buying_power"]),
            maintenance_margin=Decimal(a["maintenance_margin"]) if a.get("maintenance_margin") else None,
            margin_used=Decimal(a["initial_margin"]) if a.get("initial_margin") else None,
            options_buying_power=Decimal(a["options_buying_power"]) if a.get("options_buying_power") else None,
            day_pnl=equity - last_equity,
            as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        rows = await self._get("/v2/positions")
        result: list[Position] = []
        now = datetime.now(UTC)
        for p in rows or []:
            asset_class = {
                "us_equity": AssetClass.EQUITY,
                "us_option": AssetClass.OPTION,
                "crypto": AssetClass.CRYPTO,
            }.get(p.get("asset_class", "us_equity"), AssetClass.EQUITY)
            result.append(
                Position(
                    symbol=p["symbol"], asset_class=asset_class,
                    quantity=Decimal(p["qty"]),
                    avg_entry_price=Decimal(p["avg_entry_price"]),
                    market_value=Decimal(p["market_value"]) if p.get("market_value") else None,
                    unrealized_pnl=Decimal(p["unrealized_pl"]) if p.get("unrealized_pl") else None,
                    broker=self.name, as_of=now,
                )
            )
        return result

    async def submit_order(self, order: Order) -> Order:
        # Crypto rejects the equity order fields: the day/opg/cls TIFs return
        # HTTP 422 42210000, and extended_hours is equity-only. Remap a
        # non-crypto TIF to gtc and drop extended_hours; equities are unchanged.
        is_crypto = order.asset_class is AssetClass.CRYPTO
        if is_crypto and order.time_in_force not in _CRYPTO_TIF:
            time_in_force = "gtc"
        else:
            time_in_force = order.time_in_force.value
        body: dict[str, Any] = {
            "symbol": order.symbol.upper(),
            "side": _SIDE_MAP[order.side],
            "time_in_force": time_in_force,
            "client_order_id": order.client_order_id,
        }
        if not is_crypto:
            body["extended_hours"] = order.extended_hours
        # Alpaca uses "trailing_stop" / "stop_limit" verbatim; map enum values.
        body["type"] = {
            OrderType.MARKET: "market", OrderType.LIMIT: "limit", OrderType.STOP: "stop",
            OrderType.STOP_LIMIT: "stop_limit", OrderType.TRAILING_STOP: "trailing_stop",
        }[order.order_type]
        body["qty"] = str(order.quantity)  # fractional supported for market/day
        if order.limit_price is not None:
            body["limit_price"] = str(order.limit_price)
        if order.stop_price is not None:
            body["stop_price"] = str(order.stop_price)
        response = await self._request(
            "POST", f"{self._base}/v2/orders", headers=self._headers, json_body=body
        )
        order.broker = self.name
        order.broker_order_id = response["id"]
        order.status = _STATUS_MAP.get(response.get("status", ""), OrderStatus.SUBMITTED)
        order.updated_at = datetime.now(UTC)
        return order

    async def cancel_order(self, order: Order) -> Order:
        if not order.broker_order_id:
            raise BrokerError(self.name, "order has no broker_order_id", retryable=False)
        await self._request(
            "DELETE", f"{self._base}/v2/orders/{order.broker_order_id}", headers=self._headers
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
        response = await self._get(f"/v2/orders/{order.broker_order_id}")
        return self._merge_order(order, response)

    async def open_orders(self) -> list[Order]:
        rows = await self._get("/v2/orders", status="open", limit=100)
        return [self._row_to_order(r) for r in rows or []]

    async def recent_fills(self, *, limit: int = 50) -> list[Fill]:
        rows = await self._get("/v2/account/activities/FILL", page_size=min(limit, 100))
        fills: list[Fill] = []
        for r in rows or []:
            side = OrderSide.BUY if r.get("side") == "buy" else OrderSide.SELL
            fills.append(
                Fill(
                    order_id=r.get("order_id", ""), broker_order_id=r.get("order_id"),
                    symbol=r.get("symbol", ""), side=side,
                    quantity=Decimal(str(r.get("qty", "0"))),
                    price=Decimal(str(r.get("price", "0"))),
                    filled_at=_parse_ts(r.get("transaction_time")),
                    broker=self.name,
                )
            )
        return fills

    def _merge_order(self, order: Order, response: dict[str, Any]) -> Order:
        order.status = _STATUS_MAP.get(response.get("status", ""), order.status)
        if response.get("filled_qty"):
            order.filled_quantity = Decimal(response["filled_qty"])
        if response.get("filled_avg_price"):
            order.avg_fill_price = Decimal(response["filled_avg_price"])
        order.updated_at = datetime.now(UTC)
        return order

    def _row_to_order(self, r: dict[str, Any]) -> Order:
        side = OrderSide.BUY if r.get("side") == "buy" else OrderSide.SELL
        order_type = {
            "market": OrderType.MARKET, "limit": OrderType.LIMIT, "stop": OrderType.STOP,
            "stop_limit": OrderType.STOP_LIMIT, "trailing_stop": OrderType.TRAILING_STOP,
        }.get(r.get("type", "limit"), OrderType.LIMIT)
        order = Order(
            client_order_id=r.get("client_order_id", ""),
            broker=self.name, broker_order_id=r.get("id"),
            symbol=r.get("symbol", ""), side=side, order_type=order_type,
            quantity=Decimal(r.get("qty") or r.get("filled_qty") or "0") or Decimal("1"),
            limit_price=Decimal(r["limit_price"]) if r.get("limit_price") else None,
            stop_price=Decimal(r["stop_price"]) if r.get("stop_price") else None,
            time_in_force=TimeInForce(r.get("time_in_force", "day")),
            status=_STATUS_MAP.get(r.get("status", ""), OrderStatus.ACCEPTED),
            created_at=_parse_ts(r.get("created_at")),
            updated_at=_parse_ts(r.get("updated_at")),
        )
        if r.get("filled_qty"):
            order.filled_quantity = Decimal(r["filled_qty"])
        if r.get("filled_avg_price"):
            order.avg_fill_price = Decimal(r["filled_avg_price"])
        return order
