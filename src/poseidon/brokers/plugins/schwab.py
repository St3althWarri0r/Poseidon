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
docs/broker-setup.md (Charles Schwab section) walks through obtaining all
of these.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

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

_API = "https://api.schwabapi.com"
# The callback registered on the Schwab developer app. Schwab redirects the
# browser here with ?code=... after login+consent; nothing needs to listen on
# it (there is no local HTTPS server) — the user pastes the redirected URL back.
DEFAULT_REDIRECT_URI = "https://127.0.0.1:8182"

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
        # Schwab has no paper environment. Refuse a paper request outright
        # rather than silently trading live (mirrors PublicBroker).
        if paper:
            raise BrokerError(
                self.name,
                "Schwab has no paper environment — set `paper: false` explicitly to "
                "acknowledge live trading, or use the built-in `paper` broker for simulation",
                retryable=False,
            )
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

    # -- one-time OAuth consent (the interactive login) ------------------------
    #
    # Getting the first refresh_token needs a human browser login at Schwab.
    # These helpers drive that flow from the dashboard so the operator never
    # has to run a separate OAuth script (docs/broker-setup.md, Schwab):
    #   1. authorize_url(app_key)  -> open it; user logs in and consents
    #   2. Schwab redirects to the app's callback with ?code=...
    #   3. exchange_code(...)      -> swap the code for a refresh_token
    #   4. fetch_account_hash(...) -> the hashed account id the API needs

    @staticmethod
    def authorize_url(app_key: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
        """Schwab OAuth2 authorization URL — opening it lands on Schwab's login
        and consent screen. redirect_uri must match the app's registered
        callback (default https://127.0.0.1:8182)."""
        query = urlencode({
            "client_id": app_key,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        })
        return f"{_API}/v1/oauth/authorize?{query}"

    @staticmethod
    def extract_code(redirect_response: str) -> str:
        """Pull the authorization code out of what Schwab redirected to. Accepts
        the full pasted redirect URL (…/?code=XXXX&session=…) or a bare code."""
        value = redirect_response.strip()
        if "code=" in value:
            parsed = urlparse(value)
            codes = parse_qs(parsed.query).get("code")
            if codes and codes[0]:
                return codes[0]
        if value and "=" not in value and "/" not in value:
            return value  # already a bare code
        raise BrokerError("schwab", "no ?code= found in the pasted redirect URL", retryable=False)

    @classmethod
    async def exchange_code(cls, *, app_key: str, app_secret: str, code: str,
                            redirect_uri: str = DEFAULT_REDIRECT_URI,
                            timeout: float = 15.0) -> dict[str, Any]:
        """Exchange an authorization code for tokens. Returns the raw token
        payload (includes refresh_token). Raises BrokerAuthError on failure."""
        basic = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{_API}/v1/oauth/token",
                headers={"Authorization": f"Basic {basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "authorization_code", "code": code,
                      "redirect_uri": redirect_uri},
            )
        if resp.status_code >= 400:
            raise BrokerAuthError(
                "schwab",
                f"token exchange failed (HTTP {resp.status_code}): {resp.text[:300]} — "
                "check the app key/secret and that the code was not already used or expired",
            )
        payload: dict[str, Any] = resp.json()
        if not payload.get("refresh_token"):
            raise BrokerAuthError("schwab", "token exchange returned no refresh_token")
        return payload

    @staticmethod
    async def fetch_account_hash(access_token: str, timeout: float = 15.0) -> str:
        """Look up the hashed account id the Trader API addresses accounts by."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{_API}/trader/v1/accounts/accountNumbers",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if resp.status_code >= 400:
            raise BrokerError(
                "schwab", f"could not list account numbers (HTTP {resp.status_code})",
                retryable=False)
        rows = resp.json() or []
        if not rows or not rows[0].get("hashValue"):
            raise BrokerError("schwab", "no accounts returned for this login", retryable=False)
        return str(rows[0]["hashValue"])

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
                "redo the OAuth consent (docs/broker-setup.md, Charles Schwab) and "
                "update the refresh_token in the Account view or the vault",
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
        if order.legs:
            # This plugin only builds a SINGLE-leg orderStrategyType. Fail loud
            # rather than silently dropping the other legs and submitting a
            # naked leg whose real risk differs from what the engine vetted.
            raise BrokerError(self.name, "Schwab plugin does not support multi-leg option orders",
                              retryable=False)
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
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Connection never established: the order did not reach Schwab, so
            # a retry is safe.
            raise BrokerError(self.name, f"could not connect: {exc}", retryable=True) from exc
        except Exception as exc:
            # Sent but no clean response. Schwab has no client-order-id, so a
            # resubmit could double-fill — outcome is ambiguous, never retried.
            raise BrokerError(self.name, f"submit outcome unknown: {exc}",
                              retryable=False, ambiguous=True) from exc
        if response.status_code in (401, 403):
            raise BrokerAuthError(self.name)
        if response.status_code >= 400:
            raise BrokerError(self.name, f"order rejected HTTP {response.status_code}: {response.text[:300]}",
                              retryable=False)
        location = response.headers.get("Location", "")
        broker_order_id = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
        if not broker_order_id:
            # 201 but no order id: the order is likely live at Schwab yet cannot
            # be polled or canceled without an id, and Schwab has no client order
            # id to reconcile by. Ambiguous — mark ERROR, never auto-resubmit.
            raise BrokerError(
                self.name,
                "order accepted (HTTP 201) but no order id in Location header — "
                "verify the order at Schwab directly",
                retryable=False, ambiguous=True,
            )
        order.broker = self.name
        order.broker_order_id = broker_order_id
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
        # Schwab's /orders endpoint REQUIRES fromEnteredTime/toEnteredTime
        # (400 without them). Fetch the recent window and filter client-side to
        # the statuses we consider open, so PENDING_*/QUEUED/AWAITING_* resting
        # orders are included (a WORKING-only server filter drops them).
        now = datetime.now(UTC)
        rows = await self._request(
            "GET", f"{_API}/trader/v1/accounts/{self._account_hash}/orders",
            headers=await self._auth_headers(),
            params={
                "maxResults": 300,
                "fromEnteredTime": (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "toEnteredTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
        )
        orders: list[Order] = []
        for r in rows or []:
            status = _STATUS_MAP.get(r.get("status", ""), OrderStatus.ACCEPTED)
            if status.is_terminal:
                continue
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
