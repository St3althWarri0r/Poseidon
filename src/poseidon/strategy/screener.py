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

from ..core.config import ScreenerConfigBase
from ..core.errors import DataError
from ..data.base import DataCapability
from ..data.router import DataRouter
from ..data.universe import load_universe
from .base import pct_return

log = structlog.get_logger(__name__)

# Minimum daily bars needed to rank: a 63-day (3-month) return needs 64 closes.
_MIN_BARS = 64
# Dollar-volume lookback (20 trading days ≈ one month of ADV$).
_ADV_WINDOW = 20
# How far down the ranking the liveness probe may reach to backfill a full
# slate, as a multiple of top_n. Bounds the probe: filling 10 names never
# quotes the whole universe, however much of it is dead.
_LIVENESS_SCAN_MULTIPLE = 3
# Probes issued at once. Matches the screen's own fan-out discipline rather
# than stampeding the feed the PM is about to use.
_LIVENESS_CONCURRENCY = 6


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

    def __init__(self, config: ScreenerConfigBase, router: DataRouter,
                 *, require: DataCapability | None = None,
                 concurrency: int | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._router = router
        # ``require`` gates ``bars_multi`` to providers advertising this capability
        # (crypto passes CRYPTO so a ``/USD`` batch can never reach an equity provider);
        # ``concurrency`` bounds the per-symbol degrade fan-out. Equity passes both
        # None ⇒ routing is byte-identical to the single-screener era.
        self._require = require
        self._concurrency = concurrency
        self._now = now
        # The cache holds the full ScoredCandidate objects (not just symbols) so
        # ``ranked_candidates`` can surface each candidate's screen rationale to the
        # PM's prompt while ``select_candidates`` derives the bare symbol list — both
        # served from one screen per TTL window.
        self._cache: list[ScoredCandidate] = []
        self._cache_at = 0.0
        self._lock = asyncio.Lock()

    async def ranked_candidates(self) -> list[ScoredCandidate]:
        """Return the cached top-N :class:`ScoredCandidate` objects (symbol +
        blended-momentum metrics), re-screening when the cache TTL lapses.

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
            self._cache = ranked
            self._cache_at = self._now()
            return list(self._cache)

    async def select_candidates(self) -> list[str]:
        """Return the cached top-N ranked symbols, re-screening when the cache TTL
        lapses. Thin symbol-only view over :meth:`ranked_candidates`; NEVER raises."""
        return [c.symbol for c in await self.ranked_candidates()]


    async def filter_tradeable(
        self, ranked: list[ScoredCandidate]
    ) -> list[ScoredCandidate]:
        """Drop candidates the PM will not be able to evaluate, backfilling from
        further down the ranking.

        Ranking is built on 20-day median dollar volume — a HISTORICAL measure.
        A pair can clear that floor and still not have printed for half an hour,
        at which point the PM's ``get_quote`` is refused by the freshness policy
        and the cycle produces a data gap instead of a decision. Measured on a
        live crypto universe: median staleness 37.6s but p90 413s and a worst
        case of 1920s.

        So this asks the SAME question the PM will — ``quote(allow_delayed=
        False)`` — and keeps only the names that can answer it. Screener and
        freshness policy can then never disagree about what is tradeable.

        **Fail-open**: if the probe itself breaks (provider outage) the
        unfiltered ranking is returned. Degrading to the previous behaviour is
        strictly better than handing the PM an empty slate because the data
        layer is unwell. An empty result here means the probe WORKED and every
        candidate really is unquotable — a real answer, and the honest slate.
        """
        if not ranked:
            return []
        horizon = ranked[: max(self._config.top_n * _LIVENESS_SCAN_MULTIPLE, self._config.top_n)]
        sem = asyncio.Semaphore(_LIVENESS_CONCURRENCY)

        async def live(candidate: ScoredCandidate) -> bool:
            async with sem:
                try:
                    await self._router.quote(candidate.symbol, allow_delayed=False)
                except DataError:
                    return False  # stale or unavailable: the PM would get the same
                return True

        try:
            verdicts = await asyncio.gather(*(live(c) for c in horizon))
        except Exception:  # noqa: BLE001 - selection must never block the cycle
            log.exception("liveness probe failed; using the unfiltered ranking")
            return list(ranked[: self._config.top_n])
        kept = [c for c, ok in zip(horizon, verdicts, strict=True) if ok]
        if not kept:
            # EVERY probed candidate unquotable is far more likely to mean the
            # probe or the freshness window is misconfigured than that nothing
            # in the universe trades. Blanking the slate on that reading would
            # convert a data-layer problem into "the PM has nothing to look at",
            # so fall back to the unfiltered ranking and say so loudly. The PM's
            # own gate still refuses anything genuinely stale, and the order
            # path is unaffected either way.
            log.warning("every screened candidate was unquotable; using the "
                        "unfiltered ranking — check the freshness window and feed",
                        universe=self._config.universe, probed=len(horizon))
            return list(ranked[: self._config.top_n])
        dropped = len(horizon) - len(kept)
        if dropped:
            log.info("screener dropped unquotable candidates",
                     universe=self._config.universe, dropped=dropped,
                     probed=len(horizon), kept=len(kept))
        return kept[: self._config.top_n]

    async def _screen(self) -> list[ScoredCandidate]:
        universe = load_universe(self._config.universe)
        bars_by_symbol = await self._router.bars_multi(
            universe, timeframe="1d", limit=self._config.bars_limit,
            require=self._require, concurrency=self._concurrency,
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
        # Rank first, then keep only what the PM can actually quote. Done here
        # (once per refresh_minutes) rather than per cycle: the offenders this
        # catches are persistently illiquid pairs, not momentary gaps, so a
        # screen-time probe finds them without adding a quote burst to every
        # 60s cycle. An all-stale screen returns [] — and because an empty cache
        # is falsy, the next cycle re-screens rather than serving emptiness for
        # the whole TTL.
        top = await self.filter_tradeable(scored)
        log.info("screen complete", universe=self._config.universe, ranked=len(scored),
                 skipped=skipped, selected=len(top))
        return top
