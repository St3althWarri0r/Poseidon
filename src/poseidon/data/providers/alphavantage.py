"""Alpha Vantage provider (https://www.alphavantage.co/documentation).

Capabilities: quotes (GLOBAL_QUOTE — end-of-day/delayed on free tiers), news
with provider-computed sentiment, fundamentals (OVERVIEW + the three statement
functions, reduced to the platform's canonical line items), and insider
transactions. Alpha Vantage price data is graded DELAYED/STALE by the
freshness policy and therefore serves as a research/backfill source, never an
execution source; fundamentals/insider are slow-moving reference data.

Bars are deliberately NOT offered: the free TIME_SERIES_DAILY series is
split-UNadjusted (the adjusted series is premium-only), so serving it on
failover would silently change the price basis versus the split-adjusted
bars from Polygon/Alpaca/Twelvedata.

AV serves every numeric field as a STRING with stub literals ('None', '-',
'N/A', '') sprinkled in — ``_dec``/``_flt`` map those to None so a stub can
never raise ``InvalidOperation`` into the tool path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ...core.errors import ProviderError, ProviderRateLimitError
from ...core.models import (
    FundamentalsOverview,
    FundamentalsReport,
    InsiderTransaction,
    NewsArticle,
    Quote,
    StatementPeriod,
)
from ..base import DataCapability, MarketDataProvider

_BASE = "https://www.alphavantage.co/query"

_STUB_VALUES = frozenset({"", "-", "none", "n/a", "null"})
_MAX_STATEMENT_PERIODS = 8

# statement function -> canonical item -> AV keys in preference order
_STATEMENT_ITEMS: dict[str, dict[str, tuple[str, ...]]] = {
    "INCOME_STATEMENT": {
        "revenue": ("totalRevenue",),
        "net_income": ("netIncome",),
        "gross_profit": ("grossProfit",),
        "operating_income": ("operatingIncome",),
    },
    "BALANCE_SHEET": {
        "total_assets": ("totalAssets",),
        "total_liabilities": ("totalLiabilities",),
        "shareholder_equity": ("totalShareholderEquity",),
        "cash_and_equivalents": ("cashAndCashEquivalentsAtCarryingValue",),
        "long_term_debt": ("longTermDebtNoncurrent", "longTermDebt"),
    },
    "CASH_FLOW": {
        "operating_cashflow": ("operatingCashflow",),
        "capital_expenditures": ("capitalExpenditures",),
    },
}


def _dec(value: Any) -> Decimal | None:
    """AV numeric string -> Decimal, mapping stub literals to None so a stub
    can never raise Decimal('None') into the tool path."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _STUB_VALUES:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _flt(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _STUB_VALUES:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() in _STUB_VALUES else text


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class AlphaVantageProvider(MarketDataProvider):
    name = "alphavantage"

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({
            DataCapability.QUOTES,
            DataCapability.NEWS,
            DataCapability.FUNDAMENTALS,
            DataCapability.INSIDER,
        })

    async def _get(self, **params: Any) -> Any:
        params["apikey"] = self._api_key
        payload = await self._get_json(_BASE, params=params)
        if isinstance(payload, dict):
            if "Note" in payload or "Information" in payload:
                raise ProviderRateLimitError(self.name)
            if "Error Message" in payload:
                raise ProviderError(self.name, payload["Error Message"], retryable=False)
        return payload

    async def quote(self, symbol: str) -> Quote:
        payload = await self._get(function="GLOBAL_QUOTE", symbol=symbol.upper())
        block = payload.get("Global Quote") or {}
        price = block.get("05. price")
        trading_day = block.get("07. latest trading day")
        if not price or not trading_day:
            raise ProviderError(self.name, f"no quote for {symbol}")
        # Only a date is provided; stamp end-of-day UTC so the freshness
        # policy correctly classifies this as delayed/stale data.
        as_of = datetime.fromisoformat(trading_day).replace(hour=21, minute=0, tzinfo=UTC)
        return Quote(
            symbol=symbol,
            last=Decimal(price),
            volume=int(block["06. volume"]) if block.get("06. volume") else None,
            as_of=as_of,
            source=self.name,
        )

    async def news(self, symbols: list[str] | None = None, *, limit: int = 25) -> list[NewsArticle]:
        params: dict[str, Any] = {"function": "NEWS_SENTIMENT", "limit": min(limit, 50)}
        if symbols:
            params["tickers"] = ",".join(s.upper() for s in symbols[:5])
        payload = await self._get(**params)
        articles: list[NewsArticle] = []
        for row in payload.get("feed", []) or []:
            raw_time = row.get("time_published", "")
            try:  # format: YYYYMMDDTHHMMSS
                published = datetime.strptime(raw_time, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            except ValueError:
                continue
            sentiment = row.get("overall_sentiment_score")
            articles.append(
                NewsArticle(
                    headline=row.get("title", ""),
                    summary=row.get("summary"),
                    url=row.get("url"),
                    symbols=[t["ticker"].upper() for t in row.get("ticker_sentiment", []) or []
                             if t.get("ticker") and not str(t["ticker"]).startswith(("CRYPTO:", "FOREX:"))],
                    published_at=published,
                    source=f"{self.name}:{row.get('source', 'unknown')}",
                    sentiment=float(sentiment) if sentiment is not None else None,
                )
            )
        return articles

    # -- fundamentals ------------------------------------------------------------

    async def fundamentals(self, symbol: str) -> FundamentalsReport:
        sym = symbol.upper()
        payload = await self._get(function="OVERVIEW", symbol=sym)
        block = payload if isinstance(payload, dict) else {}
        if not block.get("Symbol"):
            # {} is AV's answer for a symbol it has no fundamentals for —
            # permanent for this request, so skip-without-penalty on failover.
            raise ProviderError(self.name, f"no fundamentals for {symbol}", retryable=False)
        overview = FundamentalsOverview(
            name=_text(block.get("Name")),
            sector=_text(block.get("Sector")),
            industry=_text(block.get("Industry")),
            description=_text(block.get("Description")),
            market_cap=_dec(block.get("MarketCapitalization")),
            revenue_ttm=_dec(block.get("RevenueTTM")),
            eps_ttm=_dec(block.get("EPS")),
            analyst_target=_dec(block.get("AnalystTargetPrice")),
            shares_outstanding=_dec(block.get("SharesOutstanding")),
            pe_ratio=_flt(block.get("PERatio")),
            forward_pe=_flt(block.get("ForwardPE")),
            peg_ratio=_flt(block.get("PEGRatio")),
            price_to_book=_flt(block.get("PriceToBookRatio")),
            ev_to_ebitda=_flt(block.get("EVToEBITDA")),
            profit_margin=_flt(block.get("ProfitMargin")),
            operating_margin=_flt(block.get("OperatingMarginTTM")),
            return_on_equity=_flt(block.get("ReturnOnEquityTTM")),
            dividend_yield=_flt(block.get("DividendYield")),
            beta=_flt(block.get("Beta")),
        )
        statements = await self._statements(sym)
        return FundamentalsReport(symbol=sym, overview=overview, statements=statements,
                                  as_of=self._now(), source=self.name)

    async def _statements(self, sym: str) -> list[StatementPeriod]:
        """INCOME_STATEMENT + BALANCE_SHEET + CASH_FLOW merged into one
        StatementPeriod per (fiscalDateEnding, annual|quarterly), curated to
        the platform's canonical item names. AV publishes no filed date or
        form — those stay None (honesty over invention)."""
        merged: dict[tuple[date, str], dict[str, Decimal]] = {}
        currencies: dict[tuple[date, str], str] = {}
        for function, item_map in _STATEMENT_ITEMS.items():
            payload = await self._get(function=function, symbol=sym)
            if not isinstance(payload, dict):
                continue
            for period, list_key in (("annual", "annualReports"),
                                     ("quarterly", "quarterlyReports")):
                for report in payload.get(list_key) or []:
                    if not isinstance(report, dict):
                        continue
                    end = _parse_date(report.get("fiscalDateEnding"))
                    if end is None:
                        continue
                    bucket = merged.setdefault((end, period), {})
                    for item, av_keys in item_map.items():
                        for av_key in av_keys:
                            value = _dec(report.get(av_key))
                            if value is not None:
                                bucket[item] = value
                                break
                    currency = _text(report.get("reportedCurrency"))
                    if currency:
                        currencies[(end, period)] = currency
        keys = sorted((k for k in merged if merged[k]), reverse=True)
        return [
            StatementPeriod(period=period, fiscal_date_ending=end, filed=None, form=None,
                            currency=currencies.get((end, period)),
                            items=merged[(end, period)])
            for end, period in keys[:_MAX_STATEMENT_PERIODS]
        ]

    # -- insider -----------------------------------------------------------------

    async def insider_transactions(self, symbol: str, *,
                                   limit: int = 20) -> list[InsiderTransaction]:
        payload = await self._get(function="INSIDER_TRANSACTIONS", symbol=symbol.upper())
        rows = payload.get("data") if isinstance(payload, dict) else None
        now = self._now()
        out: list[InsiderTransaction] = []
        # Empty data is a real answer — none reported — never an error.
        for row in (rows or [])[: max(0, limit)]:
            if not isinstance(row, dict):
                continue
            code = _text(row.get("acquisition_or_disposal"))
            shares = _dec(row.get("shares"))
            if shares is not None and code is not None and code.upper() == "D":
                shares = -shares  # disposals are negative (signed share delta)
            price = _dec(row.get("share_price"))
            if price is not None and price <= 0:
                price = None  # 0-price grants are not a market price
            out.append(InsiderTransaction(
                symbol=symbol,
                name=str(row.get("executive") or "unknown"),
                title=_text(row.get("executive_title")),
                transaction_date=_parse_date(row.get("transaction_date")),
                filing_date=None,  # AV does not publish it
                code=code,
                shares_changed=shares,
                price=price,
                as_of=now,
                source=self.name,
            ))
        return out
