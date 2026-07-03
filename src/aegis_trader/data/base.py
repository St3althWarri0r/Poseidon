"""Market data provider interface.

Each provider implements only the capabilities its upstream API actually
serves and advertises them via :meth:`capabilities`; the failover router
skips providers that cannot serve a request instead of letting them fail.

Contract for implementers:
  * Return timestamped models (``as_of`` from the provider's own timestamps
    where available, otherwise receipt time).
  * Raise :class:`ProviderError` subclasses — never return fabricated data.
  * Be stateless per request; shared state (HTTP client) is owned per
    provider instance and cleaned up in :meth:`close`.
"""

from __future__ import annotations

import abc
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

import httpx

from ..core.errors import ProviderAuthError, ProviderError, ProviderRateLimitError
from ..core.models import Bar, EarningsEvent, EconomicEvent, NewsArticle, OptionChain, Quote


class DataCapability(StrEnum):
    QUOTES = "quotes"
    BARS = "bars"
    OPTIONS = "options"
    NEWS = "news"
    EARNINGS = "earnings"
    ECONOMIC_CALENDAR = "economic_calendar"
    SECTOR = "sector"  # company sector/industry taxonomy


class MarketDataProvider(abc.ABC):
    """Base class for market data providers."""

    #: unique provider name, matches config ``providers[].name``
    name: str = ""

    def __init__(self, *, api_key: str, timeout: float = 10.0, options: dict[str, Any] | None = None) -> None:
        self._api_key = api_key
        self._options = options or {}
        self._client = httpx.AsyncClient(timeout=timeout)

    @abc.abstractmethod
    def capabilities(self) -> frozenset[DataCapability]: ...

    # Capability methods raise NotImplementedError unless overridden; the
    # router never calls a method the provider does not advertise.

    async def quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        raise NotImplementedError

    async def option_chain(self, underlying: str, *, expiration: date | None = None) -> OptionChain:
        raise NotImplementedError

    async def news(self, symbols: list[str] | None = None, *, limit: int = 25) -> list[NewsArticle]:
        raise NotImplementedError

    async def earnings(self, *, days_ahead: int = 14, symbols: list[str] | None = None) -> list[EarningsEvent]:
        raise NotImplementedError

    async def economic_calendar(self, *, days_ahead: int = 7) -> list[EconomicEvent]:
        raise NotImplementedError

    async def sector(self, symbol: str) -> str:
        """Company sector/industry classification. Raise ProviderError when
        the provider has no classification for the symbol (e.g. ETFs)."""
        raise NotImplementedError

    async def close(self) -> None:
        await self._client.aclose()

    # -- shared HTTP helpers ---------------------------------------------------

    async def _get_json(self, url: str, *, params: dict[str, Any] | None = None,
                        headers: dict[str, str] | None = None) -> Any:
        try:
            response = await self._client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"transport error: {exc}") from exc
        return self._decode(response)

    async def _post_json(self, url: str, *, json_body: dict[str, Any],
                         headers: dict[str, str] | None = None) -> Any:
        """POST helper for providers whose read endpoints take JSON bodies."""
        try:
            response = await self._client.post(url, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"transport error: {exc}") from exc
        return self._decode(response)

    def _decode(self, response: httpx.Response) -> Any:
        if response.status_code in (401, 403):
            raise ProviderAuthError(self.name)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise ProviderRateLimitError(self.name, float(retry_after) if retry_after else None)
        if response.status_code >= 400:
            raise ProviderError(self.name, f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(self.name, "invalid JSON in response") from exc

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _ts_from_epoch(value: float | int | None, *, millis: bool = False,
                       nanos: bool = False) -> datetime | None:
        if value is None:
            return None
        seconds = float(value)
        if nanos:
            seconds /= 1_000_000_000
        elif millis:
            seconds /= 1_000
        return datetime.fromtimestamp(seconds, tz=UTC)
