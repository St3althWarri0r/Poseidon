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
