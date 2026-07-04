"""Broker plugin interface.

Every brokerage integration implements :class:`Broker`. Plugins declare
their capabilities so the execution layer never routes an order a broker
cannot handle (e.g. options to an equities-only account).

Implementation contract:
  * ``connect``/``disconnect`` manage the session; ``ping`` must be cheap.
  * All account and order methods hit the broker's live API — no caching
    inside the plugin (the portfolio sync service owns caching).
  * ``submit_order`` must pass ``order.client_order_id`` to the broker when
    the API supports client order IDs; this is the duplicate-order guard.
  * Errors are raised as :class:`BrokerError` subclasses — plugins never
    swallow failures or return partial fabricated state.

Brokers without an official, self-service API ship as *documented stubs*
(:class:`UnsupportedBroker`) that explain the situation instead of resorting
to unsupported automation (see docs/broker-setup.md).
"""

from __future__ import annotations

import abc
from typing import Any

import httpx

from ..core.enums import BrokerCapability
from ..core.errors import BrokerAuthError, BrokerError, BrokerNotSupportedError
from ..core.models import (
    AccountSnapshot,
    Dividend,
    Fill,
    Order,
    Position,
    TaxLot,
)


class Broker(abc.ABC):
    """Base class for all brokerage plugins."""

    #: unique plugin name, matches config ``brokers[].name``
    name: str = ""
    #: human-readable label for docs and dashboard
    display_name: str = ""

    def __init__(self, *, credentials: dict[str, str], paper: bool = True,
                 timeout: float = 15.0, options: dict[str, Any] | None = None) -> None:
        self._credentials = credentials
        self._paper = paper
        self._options = options or {}
        self._client = httpx.AsyncClient(timeout=timeout)
        self._connected = False

    # -- lifecycle ---------------------------------------------------------------

    @abc.abstractmethod
    def capabilities(self) -> frozenset[BrokerCapability]: ...

    @abc.abstractmethod
    async def connect(self) -> None:
        """Authenticate and verify the session. Raise BrokerAuthError on failure."""

    async def disconnect(self) -> None:
        self._connected = False
        await self._client.aclose()

    async def ping(self) -> bool:
        """Cheap health check; default implementation refetches the account."""
        try:
            await self.account()
        except BrokerError:
            return False
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def is_paper(self) -> bool:
        return self._paper

    # -- account -------------------------------------------------------------------

    @abc.abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    async def positions(self) -> list[Position]: ...

    async def tax_lots(self, symbol: str | None = None) -> list[TaxLot]:
        """Optional; brokers that expose lots override this."""
        return []

    async def dividends(self, *, limit: int = 50) -> list[Dividend]:
        return []

    # -- orders ----------------------------------------------------------------------

    async def preflight(self, order: Order) -> str | None:
        """Optional broker-side pre-trade validation (buying power, margin
        impact, locate for shorts). Returns a human-readable rejection
        reason when the broker DEFINITELY refuses the order, or None when
        the order looks placeable *or the check could not be performed* —
        transport problems must not convert into false rejections; the
        authoritative answer is submit_order's."""
        return None

    @abc.abstractmethod
    async def submit_order(self, order: Order) -> Order:
        """Submit and return the order updated with broker_order_id/status."""

    @abc.abstractmethod
    async def cancel_order(self, order: Order) -> Order: ...

    @abc.abstractmethod
    async def order_status(self, order: Order) -> Order:
        """Refresh a single order's status/fills from the broker."""

    @abc.abstractmethod
    async def open_orders(self) -> list[Order]: ...

    async def recent_fills(self, *, limit: int = 50) -> list[Fill]:
        return []

    # -- shared HTTP helpers ------------------------------------------------------------

    async def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                       params: dict[str, Any] | None = None, json_body: Any | None = None,
                       data: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.request(
                method, url, headers=headers, params=params, json=json_body, data=data
            )
        except httpx.TimeoutException as exc:
            raise BrokerError(self.name, f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BrokerError(self.name, f"transport error: {exc}") from exc
        if response.status_code in (401, 403):
            raise BrokerAuthError(self.name)
        if response.status_code >= 400:
            raise BrokerError(
                self.name,
                f"HTTP {response.status_code} {method} {url}: {response.text[:300]}",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise BrokerError(self.name, "invalid JSON in response") from exc


class UnsupportedBroker(Broker):
    """Documented stub for brokerages without an official, self-service
    trading API. Every operation raises :class:`BrokerNotSupportedError`
    with an explanation and a pointer to docs/broker-setup.md. Poseidon never
    screen-scrapes or reverse-engineers private endpoints.
    """

    #: why there is no integration, shown in errors and docs
    reason: str = ""

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset()

    def _unsupported(self) -> BrokerNotSupportedError:
        return BrokerNotSupportedError(
            self.name,
            f"{self.display_name} has no official self-service trading API. {self.reason} "
            "See docs/broker-setup.md for status and alternatives.",
        )

    async def connect(self) -> None:
        raise self._unsupported()

    async def account(self) -> AccountSnapshot:
        raise self._unsupported()

    async def positions(self) -> list[Position]:
        raise self._unsupported()

    async def submit_order(self, order: Order) -> Order:
        raise self._unsupported()

    async def cancel_order(self, order: Order) -> Order:
        raise self._unsupported()

    async def order_status(self, order: Order) -> Order:
        raise self._unsupported()

    async def open_orders(self) -> list[Order]:
        raise self._unsupported()
