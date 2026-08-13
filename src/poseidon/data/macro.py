"""Macro regime context — VIX and the Treasury curve, composed for the PM.

Poseidon's :class:`DataCapability` set covers instruments: quotes, bars,
options, news, earnings, fundamentals, filings, insider. What it never had is
*market state* — whether volatility is elevated, whether the curve is
inverted. The PM was reasoning about individual names with no read on the
regime they sit in.

This module composes two keyless sources into one snapshot:

  * **CBOE** for VIX (:mod:`poseidon.data.macro`), and
  * **US Treasury** for the par yield curve and the 10Y-3M term spread
    (:mod:`poseidon.data.treasury`).

Three properties this module must hold, in order of importance:

1. **Macro is context, NEVER price truth.** Every payload carries the same
   kind of note ``read_url`` uses. VIX is an index level, not a quote, and
   nothing here may be substituted for ``get_quote``/``get_market_snapshot``.
2. **CBOE's endpoint is literally ``delayed_quotes``** — the data is delayed
   and is labelled as such in the snapshot. Poseidon grades freshness
   everywhere else; a delayed index level silently presented as current would
   be exactly the fabrication the platform bans.
3. **The legs degrade independently.** A CBOE outage must not cost the PM the
   yield curve, and vice versa; a snapshot with one leg missing reports the
   gap explicitly rather than substituting a zero.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import structlog

from ..core.errors import DataError
from . import treasury

log = structlog.get_logger(__name__)

#: CBOE's public delayed index quote. Keyless.
VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"

_CACHE_TTL = 15 * 60.0  # the feed is delayed anyway; 15 min is honest granularity
_TIMEOUT = 20.0

_module_cache: dict[str, Any] = {}
_fetch_lock = asyncio.Lock()

#: Conventional VIX regime bands. Advisory labels only — nothing keys off them.
_VIX_BANDS = ((12.0, "very_low"), (20.0, "low"), (30.0, "elevated"), (40.0, "high"))


@dataclass(frozen=True)
class VixQuote:
    """A delayed VIX index level. ``delayed`` is not configurable — CBOE's
    public endpoint serves delayed data and mislabelling it would be a lie."""

    level: float
    change_percent: float | None
    last_trade_time: str | None
    delayed: bool = True


@dataclass(frozen=True)
class MacroSnapshot:
    """Regime context. Any field may be ``None`` — a missing leg is reported
    as a gap, never as a zero."""

    vix: VixQuote | None
    vix_regime: str | None
    curve_as_of: date | None
    yield_curve: dict[str, float]
    term_spread: float | None
    curve_inverted: bool | None
    gaps: list[str]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "vix": None if self.vix is None else {
                "level": self.vix.level,
                "change_percent": self.vix.change_percent,
                "last_trade_time": self.vix.last_trade_time,
                "freshness": "delayed",  # CBOE's public feed is delayed, always
            },
            "vix_regime": self.vix_regime,
            "yield_curve": {k: round(v, 6) for k, v in sorted(self.yield_curve.items())},
            "curve_as_of": None if self.curve_as_of is None else self.curve_as_of.isoformat(),
            "term_spread_10y_3m": None if self.term_spread is None else round(self.term_spread, 6),
            "curve_inverted": self.curve_inverted,
            "note": "Macro regime CONTEXT, not price truth: VIX is a delayed "
                    "index level and the curve is a daily par-yield print. "
                    "Never substitute either for a quote — get_quote and "
                    "get_market_snapshot remain the only price sources.",
        }
        if self.gaps:
            payload["gaps"] = self.gaps
        return payload


def vix_regime(level: float) -> str:
    """Conventional band label for a VIX level. Advisory only."""
    for ceiling, label in _VIX_BANDS:
        if level < ceiling:
            return label
    return "extreme"


def parse_vix(payload: Any) -> VixQuote:
    """Extract the index level from CBOE's quote document."""
    if not isinstance(payload, dict):
        raise DataError("CBOE VIX payload is not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DataError("CBOE VIX payload has no data object")
    raw = data.get("current_price")
    if raw is None:
        raw = data.get("close")
    try:
        level = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DataError(f"CBOE VIX level is unparseable: {raw!r}") from exc
    if level <= 0:
        # VIX is a volatility index; zero or negative is a broken print, and
        # passing it through would read as "no volatility at all".
        raise DataError(f"CBOE VIX level is not positive: {level}")
    change = data.get("price_change_percent")
    try:
        change_percent = None if change is None else float(change)
    except (TypeError, ValueError):
        change_percent = None
    stamp = data.get("last_trade_time")
    return VixQuote(level=level, change_percent=change_percent,
                    last_trade_time=str(stamp) if stamp else None)


async def fetch_vix(*, transport: httpx.AsyncBaseTransport | None = None,
                    cache: dict[str, Any] | None = None) -> VixQuote:
    """Fetch the delayed VIX level, cached for :data:`_CACHE_TTL`."""
    store = _module_cache if cache is None else cache
    cached = store.get("vix")
    if cached is not None and time.monotonic() - float(store.get("vix_at", 0.0)) < _CACHE_TTL:
        return cached  # type: ignore[no-any-return]

    async with _fetch_lock:
        cached = store.get("vix")
        if cached is not None and time.monotonic() - float(store.get("vix_at", 0.0)) < _CACHE_TTL:
            return cached  # type: ignore[no-any-return]
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_TIMEOUT,
                                         follow_redirects=True) as client:
                response = await client.get(VIX_URL)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise DataError(f"CBOE VIX fetch failed: {exc}") from exc
        except ValueError as exc:
            raise DataError(f"CBOE VIX response is not JSON: {exc}") from exc

        quote = parse_vix(payload)
        store["vix"] = quote
        store["vix_at"] = time.monotonic()
        return quote


async def fetch_macro_snapshot(
        *, on: date | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: dict[str, Any] | None = None) -> MacroSnapshot:
    """Compose VIX and the Treasury curve into one regime snapshot.

    The two legs are fetched CONCURRENTLY and degrade INDEPENDENTLY: whichever
    source is down contributes a named entry to ``gaps`` while the other still
    reports. A snapshot with both legs missing is still returned — an empty
    snapshot with two gaps is honest, an exception would cost the PM its whole
    cycle over optional context.
    """
    day = on or treasury_today()
    vix_task = asyncio.create_task(fetch_vix(transport=transport, cache=cache))
    curve_task = asyncio.create_task(
        treasury.fetch_yield_curve(year=day.year, transport=transport, cache=cache))
    results = await asyncio.gather(vix_task, curve_task, return_exceptions=True)

    gaps: list[str] = []
    vix: VixQuote | None = None
    if isinstance(results[0], BaseException):
        gaps.append(f"vix_unavailable: {results[0]}")
        log.warning("macro: VIX leg unavailable", error=str(results[0]))
    else:
        vix = results[0]

    rows: list[treasury.YieldCurveRow] = []
    if isinstance(results[1], BaseException):
        gaps.append(f"yield_curve_unavailable: {results[1]}")
        log.warning("macro: Treasury leg unavailable", error=str(results[1]))
    else:
        rows = results[1]

    row = _latest_at_or_before(rows, day)
    spread = None if row is None else treasury.term_spread(row)
    return MacroSnapshot(
        vix=vix,
        vix_regime=None if vix is None else vix_regime(vix.level),
        curve_as_of=None if row is None else row.day,
        yield_curve={} if row is None else dict(row.yields),
        term_spread=spread,
        curve_inverted=None if spread is None else spread < 0,
        gaps=gaps,
    )


def treasury_today() -> date:
    from ..core.clock import utc_now

    return utc_now().date()


def _latest_at_or_before(rows: list[treasury.YieldCurveRow],
                         on: date) -> treasury.YieldCurveRow | None:
    best: treasury.YieldCurveRow | None = None
    for row in rows:  # sorted ascending by the loader
        if row.day > on:
            break
        best = row
    return best
