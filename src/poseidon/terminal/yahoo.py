"""Keyless Yahoo Finance client for the embedded terminal.

Faithful Python port of trading-terminal's lib/yahoo.ts (same endpoints
yahoo-finance2 v3 uses, same normalization quirks, same TTLs). Study data
only — never used by the trading data router or risk engine.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

import httpx
import structlog

from poseidon.core.errors import DataError

T = TypeVar("T")

_SYM_RE = re.compile(r"[^A-Z0-9.^=\-]")


def num(v: object) -> float | None:
    """Finite numbers only (bool excluded), else None — mirrors ts num()."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def frac_from_pct(v: object) -> float | None:
    """A Yahoo percent figure (79.5) as a true fraction (0.795)."""
    n = num(v)
    return n / 100 if n is not None else None


def text(v: object, fallback: str = "") -> str:
    return v if isinstance(v, str) and v else fallback


def safe_sym(s: str) -> str:
    """Restrict to characters real Yahoo tickers use (defense in depth)."""
    return _SYM_RE.sub("", s.strip().upper())[:20]


def simplify_raw(v: object) -> object:
    """Collapse Yahoo's ``{"raw": n, "fmt": "…"}`` wrappers recursively.

    yahoo-finance2 does this for its callers; the raw HTTP payloads from
    quoteSummary wrap most numerics this way.
    """
    if isinstance(v, dict):
        if "raw" in v and isinstance(v.get("raw"), (int, float, str)):
            return v["raw"]
        return {k: simplify_raw(x) for k, x in v.items()}
    if isinstance(v, list):
        return [simplify_raw(x) for x in v]
    return v


class TTLCache:
    """Tiny in-memory TTL cache; failures are never cached (mirrors ts)."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    async def get_or_fetch(self, key: str, ttl_s: float, fetch: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        hit = self._store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]  # type: ignore[no-any-return]
        value = await fetch()
        self._store[key] = (now + ttl_s, value)
        if len(self._store) > 512:
            self.evict_expired()
        return value

    def evict_expired(self) -> None:
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._store.items() if exp <= now]:
            del self._store[k]


log = structlog.get_logger(__name__)

_UA = "Mozilla/5.0 (compatible; poseidon-terminal/1.0)"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_COOKIE_URLS = ("https://fc.yahoo.com/", "https://finance.yahoo.com/quote/AAPL")


class YahooSession:
    """Shared httpx client with Yahoo's cookie+crumb handshake."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=10.0, follow_redirects=True, headers={"User-Agent": _UA})
        self._crumb: str | None = None
        self._lock = asyncio.Lock()

    async def _bootstrap(self) -> None:
        async with self._lock:
            for cookie_url in _COOKIE_URLS:
                try:
                    await self._client.get(cookie_url)  # any status; sets cookies
                    r = await self._client.get(_CRUMB_URL, headers={
                        "origin": "https://finance.yahoo.com",
                        "referer": "https://finance.yahoo.com/quote/AAPL",
                        "accept": "*/*",
                    })
                    if r.status_code == 200 and r.text and "<" not in r.text:
                        self._crumb = r.text.strip()
                        return
                except httpx.HTTPError as exc:
                    log.debug("terminal.crumb_bootstrap_failed", url=cookie_url, err=str(exc))
        raise DataError("Yahoo crumb handshake failed")

    async def get_json(self, url: str, params: dict[str, str], *,
                       needs_crumb: bool = False) -> Any:
        for attempt in (1, 2):
            q = dict(params)
            if needs_crumb:
                if self._crumb is None:
                    await self._bootstrap()
                q["crumb"] = self._crumb or ""
            try:
                r = await self._client.get(url, params=q)
            except httpx.HTTPError as exc:
                raise DataError(f"Yahoo request failed: {exc}") from exc
            if r.status_code in (401, 403) and needs_crumb and attempt == 1:
                self._crumb = None  # stale crumb — re-handshake once
                continue
            if r.status_code != 200:
                raise DataError(f"Yahoo returned HTTP {r.status_code}")
            return r.json()
        raise DataError("Yahoo auth retry exhausted")  # pragma: no cover

    async def aclose(self) -> None:
        await self._client.aclose()


_session: YahooSession | None = None


def session() -> YahooSession:
    global _session
    if _session is None:
        _session = YahooSession()
    return _session


def normalize_quote(q: Any) -> dict[str, Any]:
    g = q or {}
    avg_vol_3m = num(g.get("averageDailyVolume3Month"))
    avg_vol = avg_vol_3m if avg_vol_3m is not None else num(g.get("averageDailyVolume10Day"))
    return {
        "symbol": text(g.get("symbol")),
        "name": text(g.get("longName")) or text(g.get("shortName")) or text(g.get("symbol")),
        "quoteType": text(g.get("quoteType"), "EQUITY"),
        "currency": text(g.get("currency"), "USD"),
        "exchange": text(g.get("fullExchangeName")) or text(g.get("exchange")),
        "marketState": text(g.get("marketState"), "CLOSED"),
        "price": num(g.get("regularMarketPrice")),
        "change": num(g.get("regularMarketChange")),
        "changePercent": num(g.get("regularMarketChangePercent")),
        "previousClose": num(g.get("regularMarketPreviousClose")),
        "open": num(g.get("regularMarketOpen")),
        "dayHigh": num(g.get("regularMarketDayHigh")),
        "dayLow": num(g.get("regularMarketDayLow")),
        "volume": num(g.get("regularMarketVolume")),
        "avgVolume": avg_vol,
        "marketCap": num(g.get("marketCap")),
        "trailingPE": num(g.get("trailingPE")),
        "forwardPE": num(g.get("forwardPE")),
        "eps": num(g.get("epsTrailingTwelveMonths")),
        # Yahoo quote dividendYield is a percent (0.44 = 0.44%); store fraction.
        "dividendYield": frac_from_pct(g.get("dividendYield")),
        "beta": num(g.get("beta")),
        "fiftyTwoWeekHigh": num(g.get("fiftyTwoWeekHigh")),
        "fiftyTwoWeekLow": num(g.get("fiftyTwoWeekLow")),
        "fiftyDayAverage": num(g.get("fiftyDayAverage")),
        "twoHundredDayAverage": num(g.get("twoHundredDayAverage")),
        "sharesOutstanding": num(g.get("sharesOutstanding")),
        "postMarketPrice": num(g.get("postMarketPrice")),
        "postMarketChange": num(g.get("postMarketChange")),
        "postMarketChangePercent": num(g.get("postMarketChangePercent")),
        "preMarketPrice": num(g.get("preMarketPrice")),
        "preMarketChange": num(g.get("preMarketChange")),
        "preMarketChangePercent": num(g.get("preMarketChangePercent")),
    }


def normalize_candles(result: Any) -> list[dict[str, Any]]:
    r = result or {}
    ts: list[Any] = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    vols = quote.get("volume") or []

    candles: list[dict[str, Any]] = []
    for i, t in enumerate(ts):
        o = num(opens[i] if i < len(opens) else None)
        h = num(highs[i] if i < len(highs) else None)
        lo = num(lows[i] if i < len(lows) else None)
        close_val = num(closes[i] if i < len(closes) else None)
        tt = num(t)
        if o is None or h is None or lo is None or close_val is None or tt is None:
            continue  # Yahoo pads gaps with nulls — they'd break the chart
        v = num(vols[i] if i < len(vols) else None)
        candles.append({"time": int(tt), "open": o, "high": h, "low": lo,
                        "close": close_val, "volume": v if v is not None else 0})

    candles.sort(key=lambda c: c["time"])
    deduped: list[dict[str, Any]] = []
    for c in candles:  # strictly-ascending, last-wins (lightweight-charts rule)
        if deduped and deduped[-1]["time"] == c["time"]:
            deduped[-1] = c
        else:
            deduped.append(c)
    return deduped


def _publish_ms(t: Any) -> int | None:
    n = num(t)
    if n is not None:
        return int(n * 1000)
    if isinstance(t, str):
        try:
            return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def normalize_news(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in items or []:
        if not (n and n.get("title") and n.get("link")):
            continue
        thumbs = (n.get("thumbnail") or {}).get("resolutions")
        thumb = text(thumbs[0].get("url")) or None if isinstance(thumbs, list) and thumbs else None
        tickers = n.get("relatedTickers")
        out.append({
            "id": text(n.get("uuid")) or text(n.get("link")),
            "title": text(n.get("title")),
            "publisher": text(n.get("publisher"), "—"),
            "link": text(n.get("link")),
            "publishedAt": _publish_ms(n.get("providerPublishTime")),
            "thumbnail": thumb,
            "tickers": [str(t) for t in tickers] if isinstance(tickers, list) else [],
        })
    return out


def normalize_search(quotes: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in quotes or []:
        if not (r and r.get("symbol")):
            continue
        item: dict[str, Any] = {
            "symbol": text(r.get("symbol")),
            "name": text(r.get("longname")) or text(r.get("shortname")) or text(r.get("symbol")),
            "exchange": text(r.get("exchDisp")) or text(r.get("exchange")),
            "type": text(r.get("quoteType"), "EQUITY"),
        }
        if r.get("sector"):
            item["sector"] = text(r.get("sector"))
        if r.get("industry"):
            item["industry"] = text(r.get("industry"))
        out.append(item)
    return out


def normalize_fundamentals(sym: str, qs: Any) -> dict[str, Any]:
    d = qs or {}
    ap, sd = d.get("assetProfile") or {}, d.get("summaryDetail") or {}
    fd, ks = d.get("financialData") or {}, d.get("defaultKeyStatistics") or {}
    pr = d.get("price") or {}

    def first(*vals: float | None) -> float | None:
        for v in vals:
            if v is not None:
                return v
        return None

    return {
        "symbol": sym,
        "profile": {
            "name": text(pr.get("longName")) or text(pr.get("shortName")) or sym,
            "sector": text(ap.get("sector")) or None,
            "industry": text(ap.get("industry")) or None,
            "employees": num(ap.get("fullTimeEmployees")),
            "country": text(ap.get("country")) or None,
            "city": text(ap.get("city")) or None,
            "website": text(ap.get("website")) or None,
            "summary": text(ap.get("longBusinessSummary")) or None,
        },
        "valuation": {
            "marketCap": first(num(sd.get("marketCap")), num(pr.get("marketCap"))),
            "enterpriseValue": num(ks.get("enterpriseValue")),
            "trailingPE": num(sd.get("trailingPE")),
            "forwardPE": first(num(sd.get("forwardPE")), num(ks.get("forwardPE"))),
            "pegRatio": num(ks.get("pegRatio")),
            "priceToBook": num(ks.get("priceToBook")),
            "priceToSales": num(sd.get("priceToSalesTrailing12Months")),
            "enterpriseToEbitda": num(ks.get("enterpriseToEbitda")),
            "beta": first(num(sd.get("beta")), num(ks.get("beta"))),
        },
        "financials": {
            "revenue": num(fd.get("totalRevenue")),
            "revenueGrowth": num(fd.get("revenueGrowth")),
            "grossMargins": num(fd.get("grossMargins")),
            "operatingMargins": num(fd.get("operatingMargins")),
            "profitMargins": first(num(fd.get("profitMargins")), num(ks.get("profitMargins"))),
            "ebitda": num(fd.get("ebitda")),
            "freeCashflow": num(fd.get("freeCashflow")),
            "operatingCashflow": num(fd.get("operatingCashflow")),
            "totalCash": num(fd.get("totalCash")),
            "totalDebt": num(fd.get("totalDebt")),
            # Yahoo debtToEquity is a percent (79.55 = 0.7955x); store the ratio.
            "debtToEquity": frac_from_pct(fd.get("debtToEquity")),
            "returnOnEquity": num(fd.get("returnOnEquity")),
            "returnOnAssets": num(fd.get("returnOnAssets")),
            "currentRatio": num(fd.get("currentRatio")),
        },
        "perShare": {
            "eps": num(ks.get("trailingEps")),
            "forwardEps": num(ks.get("forwardEps")),
            "bookValue": num(ks.get("bookValue")),
            "dividendRate": num(sd.get("dividendRate")),
            "dividendYield": num(sd.get("dividendYield")),  # already a fraction here
            "payoutRatio": num(sd.get("payoutRatio")),
        },
        "targets": {
            "currentPrice": num(fd.get("currentPrice")),
            "targetMean": num(fd.get("targetMeanPrice")),
            "targetHigh": num(fd.get("targetHighPrice")),
            "targetLow": num(fd.get("targetLowPrice")),
            "recommendationKey": text(fd.get("recommendationKey")) or None,
            "numberOfAnalysts": num(fd.get("numberOfAnalystOpinions")),
        },
    }
