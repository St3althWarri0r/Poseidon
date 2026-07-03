"""Alpha Vantage provider (https://www.alphavantage.co/documentation).

Capabilities: quotes (GLOBAL_QUOTE — end-of-day/delayed on free tiers),
daily bars, and news with provider-computed sentiment. Alpha Vantage data
is graded DELAYED/STALE by the freshness policy and therefore serves as a
research/backfill source, never an execution source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ...core.errors import ProviderError, ProviderRateLimitError
from ...core.models import Bar, NewsArticle, Quote
from ..base import DataCapability, MarketDataProvider

_BASE = "https://www.alphavantage.co/query"


class AlphaVantageProvider(MarketDataProvider):
    name = "alphavantage"

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.QUOTES, DataCapability.BARS, DataCapability.NEWS})

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

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        if timeframe != "1d":
            raise ProviderError(self.name, "only 1d bars supported", retryable=False)
        payload = await self._get(
            function="TIME_SERIES_DAILY", symbol=symbol.upper(),
            outputsize="compact" if limit <= 100 else "full",
        )
        series = payload.get("Time Series (Daily)") or {}
        bars: list[Bar] = []
        for day, row in sorted(series.items())[-limit:]:
            try:
                start = datetime.fromisoformat(day).replace(tzinfo=UTC)
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        open=Decimal(row["1. open"]), high=Decimal(row["2. high"]),
                        low=Decimal(row["3. low"]), close=Decimal(row["4. close"]),
                        volume=int(row.get("5. volume", 0)),
                        start=start, end=start, source=self.name,
                    )
                )
            except (KeyError, ValueError):
                continue
        return bars

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
