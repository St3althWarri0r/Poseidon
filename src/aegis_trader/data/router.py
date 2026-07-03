"""Failover router for market data.

Providers are tried in priority order for each capability. A provider that
fails is put into a short penalty box (exponential backoff, capped) so a
flapping upstream doesn't add latency to every request. Data returned by
any provider is graded for freshness; STALE data is rejected here so it can
never reach the AI, the risk engine, or an order ticket.

If *every* capable provider fails, :class:`AllProvidersFailedError` /
:class:`DataUnavailableError` propagates and the calling cycle must not
trade — that is the "never guess" contract enforced in code.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import TypeVar

import structlog

from ..core.clock import FreshnessPolicy
from ..core.enums import DataFreshness
from ..core.errors import (
    AllProvidersFailedError,
    DataUnavailableError,
    ProviderError,
    StaleDataError,
)
from ..core.models import Bar, EarningsEvent, EconomicEvent, NewsArticle, OptionChain, Quote
from .base import DataCapability, MarketDataProvider

log = structlog.get_logger(__name__)

T = TypeVar("T")

_PENALTY_BASE = 15.0  # seconds
_PENALTY_MAX = 600.0


class _ProviderSlot:
    def __init__(self, provider: MarketDataProvider, priority: int) -> None:
        self.provider = provider
        self.priority = priority
        self.consecutive_failures = 0
        self.penalized_until = 0.0
        self.last_latency_ms: float | None = None

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.penalized_until

    def record_success(self, latency_ms: float) -> None:
        self.consecutive_failures = 0
        self.penalized_until = 0.0
        self.last_latency_ms = latency_ms

    def record_failure(self, *, retry_after: float | None = None) -> None:
        self.consecutive_failures += 1
        penalty = retry_after if retry_after is not None else min(
            _PENALTY_BASE * (2 ** (self.consecutive_failures - 1)), _PENALTY_MAX
        )
        self.penalized_until = time.monotonic() + penalty


class DataRouter:
    def __init__(self, providers: list[tuple[MarketDataProvider, int]],
                 freshness: FreshnessPolicy) -> None:
        self._slots = sorted(
            (_ProviderSlot(p, prio) for p, prio in providers), key=lambda s: s.priority
        )
        self._freshness = freshness

    @property
    def freshness(self) -> FreshnessPolicy:
        return self._freshness

    def provider_status(self) -> list[dict[str, object]]:
        """Health snapshot for the dashboard."""
        return [
            {
                "name": s.provider.name,
                "priority": s.priority,
                "available": s.available,
                "consecutive_failures": s.consecutive_failures,
                "last_latency_ms": s.last_latency_ms,
            }
            for s in self._slots
        ]

    async def close(self) -> None:
        for slot in self._slots:
            await slot.provider.close()

    # -- public API -------------------------------------------------------------

    async def quote(self, symbol: str, *, allow_delayed: bool = False) -> Quote:
        quote = await self._route(DataCapability.QUOTES, lambda p: p.quote(symbol))
        as_of = quote.as_of
        grade = self._freshness.grade(as_of)
        if grade is DataFreshness.STALE or (grade is DataFreshness.DELAYED and not allow_delayed):
            raise StaleDataError(
                f"quote for {symbol} from {quote.source} is {grade} (as_of={as_of.isoformat()}) — refusing to use it"
            )
        quote.freshness = grade
        return quote

    async def bars(self, symbol: str, *, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        return await self._route(DataCapability.BARS, lambda p: p.bars(symbol, timeframe=timeframe, limit=limit))

    async def option_chain(self, underlying: str, *, expiration: date | None = None,
                           allow_delayed: bool = False) -> OptionChain:
        chain = await self._route(
            DataCapability.OPTIONS, lambda p: p.option_chain(underlying, expiration=expiration)
        )
        grade = self._freshness.grade(chain.as_of)
        if grade is DataFreshness.STALE or (grade is DataFreshness.DELAYED and not allow_delayed):
            raise StaleDataError(
                f"option chain for {underlying} from {chain.source} is {grade} — refusing to use it"
            )
        return chain

    async def news(self, symbols: list[str] | None = None, *, limit: int = 25) -> list[NewsArticle]:
        return await self._route(DataCapability.NEWS, lambda p: p.news(symbols, limit=limit))

    async def earnings(self, *, days_ahead: int = 14,
                       symbols: list[str] | None = None) -> list[EarningsEvent]:
        return await self._route(
            DataCapability.EARNINGS, lambda p: p.earnings(days_ahead=days_ahead, symbols=symbols)
        )

    async def economic_calendar(self, *, days_ahead: int = 7) -> list[EconomicEvent]:
        return await self._route(
            DataCapability.ECONOMIC_CALENDAR, lambda p: p.economic_calendar(days_ahead=days_ahead)
        )

    # -- routing core -------------------------------------------------------------

    async def _route(self, capability: DataCapability,
                     call: Callable[[MarketDataProvider], Awaitable[T]]) -> T:
        capable = [s for s in self._slots if capability in s.provider.capabilities()]
        if not capable:
            raise DataUnavailableError(
                f"no configured provider supports '{capability}' — cannot proceed without live data"
            )
        errors: list[str] = []
        # First pass: available providers; second pass: penalized ones as a
        # last resort (better a retry than no data at all).
        for last_resort in (False, True):
            for slot in capable:
                if slot.available is last_resort and not last_resort:
                    continue
                if not last_resort and not slot.available:
                    continue
                if last_resort and slot.available:
                    continue
                started = time.monotonic()
                try:
                    result = await call(slot.provider)
                except NotImplementedError:
                    errors.append(f"{slot.provider.name}: capability not implemented")
                    continue
                except ProviderError as exc:
                    retry_after = getattr(exc, "retry_after", None)
                    slot.record_failure(retry_after=retry_after)
                    errors.append(str(exc))
                    log.warning("provider failed, failing over",
                                provider=slot.provider.name, capability=capability, error=str(exc))
                    continue
                slot.record_success((time.monotonic() - started) * 1000)
                return result
        raise AllProvidersFailedError(
            f"all providers failed for '{capability}': " + " | ".join(errors)
        )
