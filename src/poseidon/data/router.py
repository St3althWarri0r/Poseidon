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

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import TypeVar

import structlog

from ..core.clock import FreshnessPolicy
from ..core.enums import DataFreshness
from ..core.errors import (
    AllProvidersFailedError,
    DataError,
    DataUnavailableError,
    ProviderAuthError,
    ProviderError,
    StaleDataError,
)
from ..core.models import (
    Bar,
    EarningsEvent,
    EconomicEvent,
    InstrumentProfile,
    NewsArticle,
    OptionChain,
    Quote,
)
from ..core.symbols import is_crypto_symbol
from .base import DataCapability, MarketDataProvider

log = structlog.get_logger(__name__)

T = TypeVar("T")

_PENALTY_BASE = 15.0  # seconds
_PENALTY_MAX = 600.0

# Bars are historical by nature, so real-time freshness does not apply — but a
# feed that has frozen (returning weeks-old bars for a live symbol) still
# violates the live-data contract. These per-timeframe ceilings are deliberately
# generous: they never trip on a normal weekend or a long holiday gap, only on a
# clearly stalled feed. Seconds.
_MAX_BAR_AGE = {
    "1m": 2 * 86400.0, "5m": 3 * 86400.0, "15m": 3 * 86400.0,
    "1h": 5 * 86400.0, "1d": 8 * 86400.0, "1w": 45 * 86400.0,
}


def _bar_is_sound(bar: Bar) -> bool:
    """Structural OHLC sanity. A malformed bar (non-positive price, high < low,
    a high/low that doesn't bracket open/close, or negative volume) is dropped
    before it can poison indicators, the volatility halt, or VaR — a
    provider/feed glitch must never silently skew a risk calculation."""
    o, h, low, c = bar.open, bar.high, bar.low, bar.close
    if min(o, h, low, c) <= 0 or bar.volume < 0:
        return False
    if h < low:
        return False
    if h < o or h < c:  # the high must be the bar's maximum
        return False
    return not (low > o or low > c)  # the low must be the bar's minimum


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


_SECTOR_TTL = 7 * 86400.0  # classifications change on index-rebalance timescales
_SECTOR_NEGATIVE_TTL = 3600.0  # unknowns retry hourly (may be transient outage)
_SECTOR_UNKNOWN = ""  # cached negative result (e.g. ETFs have no sector)

_PROFILE_TTL = 7 * 86400.0  # instrument identity changes on corporate-action timescales
_PROFILE_NEGATIVE_TTL = 3600.0  # unresolved retries hourly (may be transient outage)


class DataRouter:
    def __init__(self, providers: list[tuple[MarketDataProvider, int]],
                 freshness: FreshnessPolicy) -> None:
        self._slots = sorted(
            (_ProviderSlot(p, prio) for p, prio in providers), key=lambda s: s.priority
        )
        self._freshness = freshness
        self._sector_cache: dict[str, tuple[str, float]] = {}
        self._profile_cache: dict[str, tuple[InstrumentProfile | None, float]] = {}

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
        is_crypto = is_crypto_symbol(symbol)
        req = DataCapability.CRYPTO if is_crypto else None
        quote = await self._route(DataCapability.QUOTES, lambda p: p.quote(symbol), require=req)
        as_of = quote.as_of
        grade = self._freshness.grade(as_of, is_crypto=is_crypto)
        if grade is DataFreshness.STALE or (grade is DataFreshness.DELAYED and not allow_delayed):
            raise StaleDataError(
                f"quote for {symbol} from {quote.source} is {grade} (as_of={as_of.isoformat()}) — refusing to use it"
            )
        quote.freshness = grade
        return quote

    async def reference_quote(self, symbol: str) -> Quote:
        """Latest quote WITHOUT the freshness gate — for display only.

        When the market is closed there is no fresh print anywhere; the honest
        answer for a human looking at a ticket is the last real trade, clearly
        labeled with its age. The quote is graded and stamped (freshness may be
        STALE) but never rejected. NO order path may use this: the risk
        engine's validate_order fetches its own strictly-graded quote.
        """
        quote = await self._route(DataCapability.QUOTES, lambda p: p.quote(symbol))
        quote.freshness = self._freshness.grade(quote.as_of, is_crypto=is_crypto_symbol(symbol))
        return quote

    async def bars(self, symbol: str, *, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        req = DataCapability.CRYPTO if is_crypto_symbol(symbol) else None
        bars = await self._route(
            DataCapability.BARS, lambda p: p.bars(symbol, timeframe=timeframe, limit=limit),
            require=req,
        )
        # Drop structurally-malformed bars at the boundary so a feed glitch
        # cannot skew an indicator/VaR/volatility calculation downstream.
        if bars:
            sound = [b for b in bars if _bar_is_sound(b)]
            if len(sound) != len(bars):
                log.warning("dropped malformed bars", symbol=symbol, timeframe=timeframe,
                            dropped=len(bars) - len(sound), source=bars[0].source)
            bars = sound
        # Reject a clearly-frozen feed (not normal weekend/holiday gaps).
        max_age = _MAX_BAR_AGE.get(timeframe)
        if bars and max_age is not None:
            age = (datetime.now(UTC) - bars[-1].end).total_seconds()
            if age > max_age:
                raise StaleDataError(
                    f"bars for {symbol} ({timeframe}) from {bars[-1].source} end "
                    f"{bars[-1].end.isoformat()} — {age / 86400:.1f}d old, feed looks frozen"
                )
        return bars

    async def bars_multi(self, symbols: list[str], *, timeframe: str = "1d",
                         limit: int = 90) -> dict[str, list[Bar]]:
        """Batched daily bars for many symbols — the screener's throughput core.

        Selects the first BARS-capable provider that implements the batch path;
        ``NotImplementedError`` tries the next provider and a ``ProviderError``
        penalizes it (existing backoff) before the next. If NO provider serves a
        batch bars endpoint (or every one is down/penalized), degrades to a
        bounded-concurrency single-symbol ``bars`` fan-out so the screener still
        works against a non-batch stack.

        NEVER raises — a symbol that cannot be served is simply absent; the same
        boundary hygiene as :meth:`bars` is applied (drop structurally-unsound
        bars, drop a symbol whose newest bar is older than ``_MAX_BAR_AGE`` — a
        frozen feed we cannot rank). The screener degrades to the watchlist on an
        empty result, so this path can never block or crash a review cycle.
        """
        if not symbols:
            return {}
        capable = [s for s in self._slots if DataCapability.BARS in s.provider.capabilities()]
        if not capable:
            return {}
        was_available = {id(s): s.available for s in capable}
        for last_resort in (False, True):
            for slot in capable:
                if was_available[id(slot)] == last_resort:
                    continue
                started = time.monotonic()
                try:
                    raw = await slot.provider.bars_multi(symbols, timeframe=timeframe, limit=limit)
                except NotImplementedError:
                    continue  # no batch path here — try the next provider, then degrade
                except ProviderError as exc:
                    if not exc.retryable and not isinstance(exc, ProviderAuthError):
                        # Permanent request/capability mismatch (e.g. unsupported
                        # timeframe): the provider is healthy — this request just
                        # cannot succeed here. Fail over WITHOUT record_failure, so
                        # its other capabilities aren't demoted into the penalty
                        # box. Mirrors the _route contract.
                        log.warning("batch bars unsupported by provider, failing over",
                                    provider=slot.provider.name, error=str(exc))
                        continue
                    slot.record_failure(retry_after=getattr(exc, "retry_after", None))
                    log.warning("batch bars provider failed, failing over",
                                provider=slot.provider.name, error=str(exc))
                    continue
                slot.record_success((time.monotonic() - started) * 1000)
                return self._sanitize_bars_multi(raw, timeframe)
        # No provider implements bars_multi (all NotImplementedError) or every
        # capable provider is down — degrade to bounded single-symbol fetches.
        return await self._bars_multi_via_single(symbols, timeframe=timeframe, limit=limit)

    def _sanitize_bars_multi(self, raw: dict[str, list[Bar]],
                             timeframe: str) -> dict[str, list[Bar]]:
        """Apply :meth:`bars` boundary hygiene per symbol: drop structurally
        unsound bars, and drop the whole symbol if it has no sound bars left or
        its newest bar is stale (frozen feed). Unlike :meth:`bars` a frozen
        symbol is dropped, not raised — one stalled name must not sink the screen."""
        max_age = _MAX_BAR_AGE.get(timeframe)
        now = datetime.now(UTC)
        out: dict[str, list[Bar]] = {}
        for symbol, bars in raw.items():
            sound = [b for b in bars if _bar_is_sound(b)]
            if not sound:
                continue
            if max_age is not None and (now - sound[-1].end).total_seconds() > max_age:
                log.info("dropping frozen symbol from batch bars", symbol=symbol,
                         timeframe=timeframe, newest=sound[-1].end.isoformat())
                continue
            out[symbol] = sound
        return out

    async def _bars_multi_via_single(self, symbols: list[str], *, timeframe: str,
                                     limit: int) -> dict[str, list[Bar]]:
        """Degrade path: bounded-concurrency single-symbol :meth:`bars` (which
        already applies sound + frozen hygiene and raises on a frozen/failed
        symbol). Per-symbol errors are swallowed so one bad name never aborts
        the screen; absent symbols are simply omitted."""
        sem = asyncio.Semaphore(16)

        async def _one(sym: str) -> tuple[str, list[Bar] | None]:
            async with sem:
                try:
                    return sym, await self.bars(sym, timeframe=timeframe, limit=limit)
                except DataError:
                    return sym, None

        results = await asyncio.gather(*(_one(s) for s in symbols))
        return {sym: bars for sym, bars in results if bars}

    async def option_chain(self, underlying: str, *, expiration: date | None = None,
                           allow_delayed: bool = False) -> OptionChain:
        chain = await self._route(
            DataCapability.OPTIONS, lambda p: p.option_chain(underlying, expiration=expiration)
        )
        grade = self._freshness.grade(chain.as_of, is_crypto=is_crypto_symbol(underlying))
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

    async def sector(self, symbol: str) -> str | None:
        """Sector classification, or None when no provider can classify the
        symbol. Unlike prices this is slow-moving reference data, so results
        (including "has no sector", e.g. ETFs) are cached for a week. None
        means *unknown*, never a guess — callers decide their own policy.
        """
        symbol = symbol.upper()
        cached = self._sector_cache.get(symbol)
        if cached is not None:
            ttl = _SECTOR_TTL if cached[0] else _SECTOR_NEGATIVE_TTL
            if time.monotonic() - cached[1] < ttl:
                return cached[0] or None
        try:
            sector = await self._route(DataCapability.SECTOR, lambda p: p.sector(symbol))
        except (AllProvidersFailedError, DataUnavailableError) as exc:
            log.info("sector unavailable", symbol=symbol, error=str(exc))
            self._sector_cache[symbol] = (_SECTOR_UNKNOWN, time.monotonic())
            return None
        self._sector_cache[symbol] = (sector, time.monotonic())
        return sector

    async def profile(self, symbol: str) -> InstrumentProfile | None:
        """Resolved instrument identity, or None when no provider can resolve
        the symbol. Like sector() this is slow-moving reference data, so
        results are cached for a week and unresolved symbols retried hourly.
        None means *unresolved*, never a guess — callers decide their own
        policy (ticker-only identity downstream).
        """
        symbol = symbol.upper()
        cached = self._profile_cache.get(symbol)
        if cached is not None:
            ttl = _PROFILE_TTL if cached[0] is not None else _PROFILE_NEGATIVE_TTL
            if time.monotonic() - cached[1] < ttl:
                return cached[0]
        try:
            prof = await self._route(DataCapability.PROFILE, lambda p: p.profile(symbol))
        except (AllProvidersFailedError, DataUnavailableError) as exc:
            log.info("profile unavailable", symbol=symbol, error=str(exc))
            self._profile_cache[symbol] = (None, time.monotonic())
            return None
        self._profile_cache[symbol] = (prof, time.monotonic())
        return prof

    # -- routing core -------------------------------------------------------------

    async def _route(self, capability: DataCapability,
                     call: Callable[[MarketDataProvider], Awaitable[T]],
                     *, require: DataCapability | None = None) -> T:
        # `require` is an extra capability a provider must ALSO advertise to be
        # eligible (e.g. CRYPTO for a BASE/USD symbol), so a crypto request can
        # never reach an equity-only provider. Equity requests pass require=None
        # and the capable set is byte-for-byte unchanged.
        capable = [
            s for s in self._slots
            if capability in s.provider.capabilities()
            and (require is None or require in s.provider.capabilities())
        ]
        if not capable:
            detail = f"'{capability}'" if require is None else f"'{capability}' with '{require}'"
            raise DataUnavailableError(
                f"no configured provider supports {detail} — cannot proceed without live data"
            )
        errors: list[str] = []
        # Snapshot availability ONCE up front. A provider that fails in the
        # first pass gets penalized (record_failure sets penalized_until), but
        # that must not make it re-selected in the second pass within the same
        # request — otherwise a just-failed/rate-limited provider is re-hit and
        # its backoff double-counts. First pass: providers available now;
        # second pass: the rest, as a last resort (better a retry than none).
        was_available = {id(s): s.available for s in capable}
        for last_resort in (False, True):
            for slot in capable:
                if was_available[id(slot)] == last_resort:
                    continue  # available slots run in pass 1, penalized in pass 2
                started = time.monotonic()
                try:
                    result = await call(slot.provider)
                except NotImplementedError:
                    errors.append(f"{slot.provider.name}: capability not implemented")
                    continue
                except ProviderError as exc:
                    if not exc.retryable and not isinstance(exc, ProviderAuthError):
                        # Permanent request/capability mismatch (e.g. "only 1d
                        # bars supported"): the provider is healthy — this
                        # request can just never succeed there. Skip like
                        # NotImplementedError; the penalty box is per-provider,
                        # so record_failure would demote its healthy
                        # capabilities behind lower-priority providers.
                        errors.append(str(exc))
                        continue
                    retry_after = getattr(exc, "retry_after", None)
                    slot.record_failure(retry_after=retry_after)
                    errors.append(str(exc))
                    # Auth failures disable every capability and never
                    # self-heal — surface them at error level.
                    log_fn = log.error if isinstance(exc, ProviderAuthError) else log.warning
                    log_fn("provider failed, failing over",
                           provider=slot.provider.name, capability=capability, error=str(exc))
                    continue
                except (ArithmeticError, TypeError, ValueError) as exc:
                    # A provider returned an unparseable value (e.g.
                    # Decimal("N/A") -> InvalidOperation, float(None) ->
                    # TypeError, or a model ValidationError on inf/nan). These
                    # are NOT PoseidonErrors, so without this they would escape
                    # failover entirely and violate the DataError contract.
                    # Treat as a provider failure and fail over.
                    slot.record_failure()
                    errors.append(f"{slot.provider.name}: malformed data ({exc})")
                    log.warning("provider returned malformed data, failing over",
                                provider=slot.provider.name, capability=capability, error=str(exc))
                    continue
                slot.record_success((time.monotonic() - started) * 1000)
                return result
        raise AllProvidersFailedError(
            f"all providers failed for '{capability}': " + " | ".join(errors)
        )
