"""Market screener — blended-momentum ranking over a broad index universe.

Each cycle the screener cheaply ranks ~500 S&P 500 names from batched daily bars
and hands the AI the top-N to deep-analyze (classic screen-then-analyze). It is
**advisory selection only**: it picks WHICH symbols the AI evaluates and NEVER
decides whether to trade — every candidate still flows AI → RiskEngine → broker
unchanged. Off by default (``ScreenerConfig.enabled=False`` ⇒ ``[]`` ⇒ the cycle
is byte-identical to today).

Ranking is **blended momentum** ``0.6·r_1m + 0.4·r_3m`` behind a **median 20-day
dollar-volume floor** — cheap (closes + volume, no quotes) and built entirely on
the pure ``strategy.base``/``indicators`` helpers. Ranking math is ``float``
(the indicator convention; no money reaches an order from here); the ``Decimal``
liquidity threshold is cast to ``float`` only at the compare.

The ranked list is cached for ``refresh_minutes`` so a full screen runs a few
times an hour, not every cycle. ``select_candidates`` **never raises** — a screen
failure returns the last good cache (or ``[]``), so the caller degrades to the
watchlist and the review cycle is never blocked or crashed. The screener imports
only ``data.universe`` (its own severed universe copy), ``data.router`` and the
pure ``strategy.base`` helpers — never ``research``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from statistics import median

import structlog

from ..core.config import ScreenerConfig
from ..data.router import DataRouter
from ..data.universe import load_universe
from .base import pct_return

log = structlog.get_logger(__name__)

# Minimum daily bars needed to rank: a 63-day (3-month) return needs 64 closes.
_MIN_BARS = 64
# Dollar-volume lookback (20 trading days ≈ one month of ADV$).
_ADV_WINDOW = 20


@dataclass(frozen=True)
class ScoredCandidate:
    """A ranked screener candidate. ``score`` is blended momentum; the return and
    dollar-volume fields are kept for logging/inspection (never fed to an order)."""

    symbol: str
    score: float
    dollar_volume: float
    r_1m: float
    r_3m: float


class MarketScreener:
    """Ranks a broad universe by blended momentum and caches the top-N.

    The clock is injectable (``now``) so the cache TTL is deterministic in tests;
    it defaults to :func:`time.monotonic`. A single :class:`asyncio.Lock`
    serializes screens so concurrent review cycles share one result rather than
    stampeding the data feed.
    """

    def __init__(self, config: ScreenerConfig, router: DataRouter,
                 *, now: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._router = router
        self._now = now
        self._cache: list[str] = []
        self._cache_at = 0.0
        self._lock = asyncio.Lock()

    async def select_candidates(self) -> list[str]:
        """Return the cached top-N ranked symbols, re-screening when the cache TTL
        lapses.

        NEVER raises: a screen failure returns the last good cache (or ``[]``), so
        the caller degrades to the watchlist and the cycle is never blocked.
        """
        if not self._config.enabled:
            return []
        async with self._lock:  # one screen at a time; concurrent cycles share it
            if self._cache and self._now() - self._cache_at < self._config.refresh_minutes * 60:
                return list(self._cache)
            try:
                ranked = await self._screen()
            except Exception:  # noqa: BLE001 - screening must never block the cycle
                log.exception("screener failed; reusing last candidates")
                return list(self._cache)
            self._cache = [c.symbol for c in ranked]
            self._cache_at = self._now()
            return list(self._cache)

    async def _screen(self) -> list[ScoredCandidate]:
        universe = load_universe(self._config.universe)
        bars_by_symbol = await self._router.bars_multi(
            universe, timeframe="1d", limit=self._config.bars_limit
        )
        floor = float(self._config.min_dollar_volume)  # Decimal cfg → float compare
        scored: list[ScoredCandidate] = []
        skipped = 0
        for symbol, bars in bars_by_symbol.items():
            if len(bars) < _MIN_BARS:  # need a 63d return + 1
                skipped += 1
                continue
            closes = [float(b.close) for b in bars]
            adv = median(
                [closes[i] * bars[i].volume for i in range(len(bars))][-_ADV_WINDOW:]
            )
            if adv < floor:  # liquidity floor
                skipped += 1
                continue
            r1m = pct_return(closes, 21)
            r3m = pct_return(closes, 63)
            if r1m is None or r3m is None:
                skipped += 1
                continue
            scored.append(
                ScoredCandidate(symbol, 0.6 * r1m + 0.4 * r3m, adv, r1m, r3m)
            )
        scored.sort(key=lambda c: c.score, reverse=True)
        top = scored[: self._config.top_n]
        log.info("screen complete", universe=self._config.universe, ranked=len(scored),
                 skipped=skipped, selected=len(top))
        return top
