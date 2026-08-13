"""Ken French Data Library loader — canonical factor returns.

Source for the **Mkt-RF / SMB / HML factor returns**, which let the factor lab
report a candidate's *residual* alpha: the return that market, size and value
exposure do not already explain. Keyless, published by Dartmouth (Fama/French).
We fetch it at runtime and never vendor it — the series are factual return
data, but the file itself stays upstream where it is kept current.

**This module is not Poseidon's risk-free rate.** The ``RF`` column looks like
the obvious source and was measured before being rejected: it carries two
decimals of a DAILY rate, so it quantizes to 2.52%/yr steps and 2023, 2024 and
2025 all report exactly 5.04%. :mod:`poseidon.data.treasury` serves the rate
from the 3-month par yield instead, at ±0.005pp. ``RF`` is still exported here
because factor regressions need excess returns computed against the *same* RF
that Mkt-RF was constructed with — internal consistency beats absolute
accuracy in that one context, and 1bp/day rounding is second-order against
daily factor moves of ~1%.

File shape (verified against the live file, 202606 CRSP build):

    This file was created by using the 202606 CRSP database.   <- preamble
    ...
    ,Mkt-RF,SMB,HML,RF                                         <- bare header
    19260701,    0.09,   -0.25,   -0.27,    0.01               <- YYYYMMDD
    ...
    Copyright 2026 Eugene F. Fama and Kenneth R. French        <- footer

Three details the parser must get right:

  * values are **percent** — 0.09 means 0.0009;
  * ``RF`` is a **daily simple rate**, so annualizing multiplies by 252 (the
    same constant ``stats.sharpe_ratio`` divides by);
  * the library publishes roughly six weeks in arrears, so any live-adjacent
    date is past the end of the series. :func:`risk_free_annual_on` carries the
    last known rate forward rather than falling back to 0.0, which would
    quietly restore the bug this module exists to fix.
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import structlog

from ..core.errors import DataError

log = structlog.get_logger(__name__)

#: The daily 3-factor file. Keyless, ~178 KB, refreshed monthly.
FACTORS_DAILY_URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
                     "ftp/F-F_Research_Data_Factors_daily_CSV.zip")

_TRADING_DAYS = 252.0
_CACHE_TTL = 24 * 60 * 60.0  # the file changes monthly; a daily refetch is plenty
_TIMEOUT = 30.0
_MAX_BYTES = 8 * 1024 * 1024

_module_cache: dict[str, Any] = {}
_fetch_lock = asyncio.Lock()


@dataclass(frozen=True)
class FactorRow:
    """One trading day of Fama-French factors, as decimals (not percent)."""

    day: date
    mkt_rf: float
    smb: float
    hml: float
    rf: float  # daily simple rate


def parse_factor_csv(text: str) -> list[FactorRow]:
    """Parse the library CSV into a date-sorted series.

    Tolerant by construction: the file carries a prose preamble, a copyright
    footer, blank lines, and — in the monthly/annual variants — a second table
    under a repeated header whose keys are four-digit YEARS. Anything that is
    not a ``YYYYMMDD`` row with five parseable columns is skipped rather than
    raising, so an upstream format tweak degrades to fewer rows instead of
    taking the cycle down.
    """
    rows: list[FactorRow] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        stamp = parts[0]
        if len(stamp) != 8 or not stamp.isdigit():
            continue  # preamble, header, or an annual-section YEAR key
        try:
            day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
            values = [float(p) / 100.0 for p in parts[1:]]  # percent -> decimal
        except ValueError:
            continue
        rows.append(FactorRow(day=day, mkt_rf=values[0], smb=values[1],
                              hml=values[2], rf=values[3]))
    rows.sort(key=lambda r: r.day)
    return rows


def risk_free_annual_on(rows: list[FactorRow], on: date) -> float:
    """Annualized risk-free rate from the ``RF`` column.

    **Degrade path only.** :func:`poseidon.data.treasury.risk_free_annual_on`
    is the primary rate; this exists so an unreachable Treasury feed falls back
    to a coarse-but-real rate rather than to 0.0, which is the bug the rate was
    introduced to fix. Quantized to 2.52%/yr steps — see the module docstring.

    Returns the most recent row at or before ``on``, carried forward: the
    library publishes ~6 weeks in arrears, so live-adjacent dates are always
    past the end of the series. Before the series begins there is genuinely no
    data, so the answer is 0.0.
    """
    best: FactorRow | None = None
    for row in rows:  # rows are sorted; the last match at-or-before wins
        if row.day > on:
            break
        best = row
    if best is None:
        return 0.0
    return best.rf * _TRADING_DAYS


def _decode_zip(payload: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        names = archive.namelist()
        if not names:
            raise DataError("Fama-French archive is empty")
        # latin-1: the file carries occasional non-UTF8 bytes in the preamble.
        return archive.read(names[0]).decode("latin-1")
    except zipfile.BadZipFile as exc:
        # A maintenance page or a redirect to HTML lands here.
        raise DataError(f"Fama-French payload is not a zip archive: {exc}") from exc


async def fetch_factor_rows(*, url: str = FACTORS_DAILY_URL,
                            transport: httpx.AsyncBaseTransport | None = None,
                            cache: dict[str, Any] | None = None) -> list[FactorRow]:
    """Fetch and parse the daily factor series, cached for :data:`_CACHE_TTL`.

    ``transport`` and ``cache`` are injectable so tests exercise the real
    unzip/parse path offline.
    """
    store = _module_cache if cache is None else cache
    now = time.monotonic()
    cached = store.get("rows")
    if cached is not None and now - float(store.get("at", 0.0)) < _CACHE_TTL:
        return list(cached)

    async with _fetch_lock:
        # Double-checked: a second caller that queued on the lock must not
        # re-download the archive the first one just fetched.
        cached = store.get("rows")
        if cached is not None and time.monotonic() - float(store.get("at", 0.0)) < _CACHE_TTL:
            return list(cached)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_TIMEOUT,
                                         follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.content
        except httpx.HTTPError as exc:
            raise DataError(f"Fama-French fetch failed: {exc}") from exc
        if len(payload) > _MAX_BYTES:
            raise DataError(f"Fama-French payload too large: {len(payload)} bytes")

        rows = parse_factor_csv(_decode_zip(payload))
        if not rows:
            raise DataError("Fama-French archive parsed to zero rows")
        store["rows"] = rows
        store["at"] = time.monotonic()
        log.info("fama-french factors loaded", rows=len(rows),
                 first=str(rows[0].day), last=str(rows[-1].day))
        return list(rows)
