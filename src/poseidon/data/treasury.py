"""US Treasury par yield curve — the risk-free rate and the first macro series.

Two jobs:

  * **the risk-free rate** for Sharpe/Sortino. ``backtest/stats.py`` assumed
    rf=0, which flatters every result;
  * **macro state** — the curve itself, and the 10Y-3M term spread, which is
    regime context the PM had no source for.

Keyless, no account, one-day publication lag.

*Why not Ken French's RF column?* It was the obvious candidate — it aligns
exactly with the factor returns in :mod:`poseidon.data.famafrench` — but it is
published as two decimals of a DAILY rate, so it quantizes to 2.52%/yr steps.
Empirically 2023, 2024 and 2025 all report exactly 5.04%. Treasury publishes
two decimals of the ANNUAL rate, i.e. ±0.005pp against ±1.26pp. Ken French's RF
remains the right choice inside factor regressions, where consistency with
Mkt-RF matters more than absolute accuracy.

The feed is Atom/OData, one ``<entry>`` per business day, parameterized by
year — a backtest spanning several years fetches one document per year.
Yields are annual percent (``3.87`` -> ``0.0387``); absent tenors are omitted
rather than defaulted, because a missing yield is not a 0% yield.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import structlog

from ..core.errors import DataError

log = structlog.get_logger(__name__)

YIELD_CURVE_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                   "interest-rates/pages/xml")

_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
}

#: Treasury's element name -> the short tenor label we expose.
_TENORS = {
    "BC_1MONTH": "1M", "BC_1_5MONTH": "1.5M", "BC_2MONTH": "2M",
    "BC_3MONTH": "3M", "BC_4MONTH": "4M", "BC_6MONTH": "6M",
    "BC_1YEAR": "1Y", "BC_2YEAR": "2Y", "BC_3YEAR": "3Y", "BC_5YEAR": "5Y",
    "BC_7YEAR": "7Y", "BC_10YEAR": "10Y", "BC_20YEAR": "20Y", "BC_30YEAR": "30Y",
}

#: The conventional Sharpe risk-free proxy.
TENOR_3M = "3M"
TENOR_10Y = "10Y"

_CACHE_TTL = 6 * 60 * 60.0  # published once daily; four refreshes a day is ample
_TIMEOUT = 30.0

_module_cache: dict[str, Any] = {}
_fetch_lock = asyncio.Lock()


@dataclass(frozen=True)
class YieldCurveRow:
    """One business day's par yield curve, as decimals (not percent)."""

    day: date
    yields: dict[str, float]


def parse_yield_curve_xml(raw: str) -> list[YieldCurveRow]:
    """Parse the Atom/OData feed into a date-sorted series.

    Raises :class:`DataError` if the document itself is unparseable — that is
    an upstream outage, not a data gap. Individual bad *entries* are skipped.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DataError(f"Treasury yield-curve XML is malformed: {exc}") from exc

    rows: list[YieldCurveRow] = []
    for entry in root.findall("a:entry", _NS):
        props = entry.find("a:content/m:properties", _NS)
        if props is None:
            continue
        stamp = props.find("d:NEW_DATE", _NS)
        if stamp is None or not (stamp.text or "").strip():
            continue
        try:
            day = date.fromisoformat((stamp.text or "").strip()[:10])
        except ValueError:
            continue
        yields: dict[str, float] = {}
        for element, label in _TENORS.items():
            node = props.find(f"d:{element}", _NS)
            text = "" if node is None else (node.text or "").strip()
            if not text:
                continue  # an untraded tenor is absent, never zero
            try:
                yields[label] = float(text) / 100.0  # percent -> decimal
            except ValueError:
                continue
        if yields:
            rows.append(YieldCurveRow(day=day, yields=yields))
    rows.sort(key=lambda r: r.day)
    return rows


def risk_free_annual_on(rows: list[YieldCurveRow], on: date) -> float:
    """Annualized 3-month yield in effect on ``on``.

    Uses the most recent curve at or before ``on``, carried forward across
    weekends, holidays and the publication lag. Never looks ahead: a backtest
    bar cannot see a rate published after it. Before the series there is no
    data, so the answer is 0.0.
    """
    best: YieldCurveRow | None = None
    for row in rows:  # sorted; the last match at-or-before wins
        if row.day > on:
            break
        if TENOR_3M in row.yields:
            best = row
    return 0.0 if best is None else best.yields[TENOR_3M]


def term_spread(row: YieldCurveRow) -> float | None:
    """10Y minus 3M — negative means an inverted curve. ``None`` if a leg is
    missing, which is a gap and must not be reported as a flat curve."""
    if TENOR_10Y not in row.yields or TENOR_3M not in row.yields:
        return None
    return row.yields[TENOR_10Y] - row.yields[TENOR_3M]


async def fetch_yield_curve(*, year: int,
                            transport: httpx.AsyncBaseTransport | None = None,
                            cache: dict[str, Any] | None = None) -> list[YieldCurveRow]:
    """Fetch and parse one calendar year of the curve, cached per year."""
    store = _module_cache if cache is None else cache
    key = f"rows:{year}"
    at_key = f"at:{year}"
    cached = store.get(key)
    if cached is not None and time.monotonic() - float(store.get(at_key, 0.0)) < _CACHE_TTL:
        return list(cached)

    async with _fetch_lock:
        cached = store.get(key)
        if cached is not None and time.monotonic() - float(store.get(at_key, 0.0)) < _CACHE_TTL:
            return list(cached)
        params = {"data": "daily_treasury_yield_curve",
                  "field_tdr_date_value": str(year)}
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_TIMEOUT,
                                         follow_redirects=True) as client:
                response = await client.get(YIELD_CURVE_URL, params=params)
                response.raise_for_status()
                text = response.text
        except httpx.HTTPError as exc:
            raise DataError(f"Treasury yield-curve fetch failed: {exc}") from exc

        rows = parse_yield_curve_xml(text)
        store[key] = rows
        store[at_key] = time.monotonic()
        log.info("treasury yield curve loaded", year=year, rows=len(rows),
                 last=str(rows[-1].day) if rows else None)
        return list(rows)
