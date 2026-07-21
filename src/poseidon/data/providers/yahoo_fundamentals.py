"""Yahoo Finance fundamentals provider (quoteSummary; keyless, overview-only).

A secondary OVERVIEW source behind SEC EDGAR / Alpha Vantage: company profile,
market cap, TTM revenue/EPS, analyst target, and valuation ratios from the
``v10/finance/quoteSummary`` endpoint (``formatted=false``). ``statements`` is
always empty — Yahoo's statement history is not served on this seam.

The endpoint knowledge (cookie+crumb handshake with one re-handshake retry on
401/403, module list) is ported here PROVIDER-LOCALLY from the terminal's
implementation. ``poseidon.terminal.yahoo`` is display-only ("Study data only —
never used by the trading data router") and is deliberately NOT imported; this
provider owns its own handshake state on its own HTTP client, with no disk
persistence and no module singleton.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ...core.errors import ProviderAuthError, ProviderError
from ...core.models import FundamentalsOverview, FundamentalsReport
from ..base import DataCapability, MarketDataProvider

_UA = "Mozilla/5.0 (compatible; poseidon-data/1.0)"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_COOKIE_URLS = ("https://fc.yahoo.com/", "https://finance.yahoo.com/quote/AAPL")
_QUOTE_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
_MODULES = "assetProfile,summaryDetail,financialData,defaultKeyStatistics,price"


def _plain(value: Any) -> Any:
    """Unwrap a residual ``{'raw': n}`` envelope. ``formatted=false`` serves
    plain values, but the shape is defended anyway."""
    if isinstance(value, dict):
        return value.get("raw")
    return value


def _dec(value: Any) -> Decimal | None:
    value = _plain(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _flt(value: Any) -> float | None:
    value = _plain(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    value = _plain(value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first(*values: Any) -> Any:
    """First non-None value (never ``or``: Decimal('0') is falsy)."""
    for v in values:
        if v is not None:
            return v
    return None


class YahooFundamentalsProvider(MarketDataProvider):
    name = "yahoo_fundamentals"

    def __init__(self, *, api_key: str, timeout: float = 10.0,
                 options: dict[str, Any] | None = None) -> None:
        super().__init__(api_key=api_key, timeout=timeout, options=options)
        self._crumb: str | None = None
        self._handshake_lock = asyncio.Lock()

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.FUNDAMENTALS})

    async def _handshake(self) -> None:
        """Cookie + crumb bootstrap on this provider's own client."""
        async with self._handshake_lock:
            if self._crumb is not None:
                return  # another coroutine already completed the handshake
            for cookie_url in _COOKIE_URLS:
                try:
                    # Any status is fine — the point is the Set-Cookie.
                    await self._client.get(cookie_url, headers={"User-Agent": _UA},
                                           follow_redirects=True)
                    response = await self._client.get(_CRUMB_URL, headers={
                        "User-Agent": _UA,
                        "origin": "https://finance.yahoo.com",
                        "referer": "https://finance.yahoo.com/quote/AAPL",
                        "accept": "*/*",
                    })
                except httpx.HTTPError:
                    continue
                if response.status_code == 200 and response.text and "<" not in response.text:
                    self._crumb = response.text.strip()
                    return
            raise ProviderError(self.name, "Yahoo crumb handshake failed")

    async def _quote_summary(self, symbol: str) -> Any:
        for attempt in (1, 2):
            if self._crumb is None:
                await self._handshake()
            try:
                return await self._get_json(
                    _QUOTE_SUMMARY_URL.format(symbol=symbol),
                    params={"modules": _MODULES, "formatted": "false",
                            "crumb": self._crumb or ""},
                    headers={"User-Agent": _UA},
                )
            except ProviderAuthError:
                if attempt == 2:
                    raise
                self._crumb = None  # stale crumb — re-handshake exactly once
        raise ProviderError(self.name, "auth retry exhausted")  # pragma: no cover

    async def fundamentals(self, symbol: str) -> FundamentalsReport:
        sym = symbol.strip().upper()
        payload = await self._quote_summary(sym)
        summary = payload.get("quoteSummary") if isinstance(payload, dict) else None
        results = summary.get("result") if isinstance(summary, dict) else None
        block = results[0] if isinstance(results, list) and results else None
        if not isinstance(block, dict) or not block:
            raise ProviderError(self.name, f"no fundamentals for {symbol}", retryable=False)
        profile = block.get("assetProfile") or {}
        detail = block.get("summaryDetail") or {}
        financial = block.get("financialData") or {}
        key_stats = block.get("defaultKeyStatistics") or {}
        price = block.get("price") or {}
        overview = FundamentalsOverview(
            name=_first(_text(price.get("longName")), _text(price.get("shortName"))),
            sector=_text(profile.get("sector")),
            industry=_text(profile.get("industry")),
            description=_text(profile.get("longBusinessSummary")),
            market_cap=_first(_dec(detail.get("marketCap")), _dec(price.get("marketCap"))),
            revenue_ttm=_dec(financial.get("totalRevenue")),
            eps_ttm=_dec(key_stats.get("trailingEps")),
            analyst_target=_dec(financial.get("targetMeanPrice")),
            shares_outstanding=_dec(key_stats.get("sharesOutstanding")),
            pe_ratio=_flt(detail.get("trailingPE")),
            forward_pe=_first(_flt(detail.get("forwardPE")), _flt(key_stats.get("forwardPE"))),
            peg_ratio=_flt(key_stats.get("pegRatio")),
            price_to_book=_flt(key_stats.get("priceToBook")),
            ev_to_ebitda=_flt(key_stats.get("enterpriseToEbitda")),
            profit_margin=_first(_flt(financial.get("profitMargins")),
                                 _flt(key_stats.get("profitMargins"))),
            operating_margin=_flt(financial.get("operatingMargins")),
            return_on_equity=_flt(financial.get("returnOnEquity")),
            dividend_yield=_flt(detail.get("dividendYield")),
            beta=_first(_flt(detail.get("beta")), _flt(key_stats.get("beta"))),
        )
        # statements=[] — overview-only secondary source; the filed statement
        # history comes from SEC EDGAR / Alpha Vantage.
        return FundamentalsReport(symbol=sym, overview=overview, statements=[],
                                  as_of=self._now(), source=self.name)
