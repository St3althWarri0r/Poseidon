"""Polygon.io provider (https://polygon.io/docs).

Capabilities: real-time/last quotes, aggregate bars, option chain snapshots,
news. Authentication: API key as query parameter.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ...core.enums import OptionRight
from ...core.errors import ProviderError
from ...core.models import Bar, Greeks, NewsArticle, OptionChain, OptionContract, Quote
from ..base import DataCapability, MarketDataProvider

_BASE = "https://api.polygon.io"

_TIMEFRAMES = {
    "1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"),
    "1h": (1, "hour"), "1d": (1, "day"), "1w": (1, "week"),
}


class PolygonProvider(MarketDataProvider):
    name = "polygon"

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset(
            {DataCapability.QUOTES, DataCapability.BARS, DataCapability.OPTIONS, DataCapability.NEWS}
        )

    async def _get(self, path: str, **params: Any) -> Any:
        params["apiKey"] = self._api_key
        return await self._get_json(f"{_BASE}{path}", params=params)

    async def quote(self, symbol: str) -> Quote:
        payload = await self._get(f"/v2/last/nbbo/{symbol.upper()}")
        results = payload.get("results")
        if not results:
            raise ProviderError(self.name, f"no NBBO for {symbol}")
        as_of = self._ts_from_epoch(results.get("t"), nanos=True) or self._now()
        return Quote(
            symbol=symbol,
            bid=Decimal(str(results["p"])) if results.get("p") is not None else None,
            ask=Decimal(str(results["P"])) if results.get("P") is not None else None,
            bid_size=results.get("s"),
            ask_size=results.get("S"),
            as_of=as_of,
            source=self.name,
        )

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        if timeframe not in _TIMEFRAMES:
            raise ProviderError(self.name, f"unsupported timeframe {timeframe}", retryable=False)
        multiplier, span = _TIMEFRAMES[timeframe]
        end = datetime.now(UTC).date()
        start = end - timedelta(days=730 if span in ("day", "week") else 30)
        payload = await self._get(
            f"/v2/aggs/ticker/{symbol.upper()}/range/{multiplier}/{span}/{start}/{end}",
            adjusted="true", sort="desc", limit=limit,
        )
        bars: list[Bar] = []
        for row in payload.get("results", []) or []:
            start_ts = self._ts_from_epoch(row["t"], millis=True)
            assert start_ts is not None
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    open=Decimal(str(row["o"])), high=Decimal(str(row["h"])),
                    low=Decimal(str(row["l"])), close=Decimal(str(row["c"])),
                    volume=int(row.get("v", 0)),
                    start=start_ts, end=start_ts, source=self.name,
                )
            )
        bars.reverse()  # chronological
        return bars

    async def option_chain(self, underlying: str, *, expiration: date | None = None) -> OptionChain:
        params: dict[str, Any] = {"limit": 250}
        if expiration:
            params["expiration_date"] = expiration.isoformat()
        payload = await self._get(f"/v3/snapshot/options/{underlying.upper()}", **params)
        contracts: list[OptionContract] = []
        expirations: set[date] = set()
        now = self._now()
        for row in payload.get("results", []) or []:
            details = row.get("details", {})
            quote_block = row.get("last_quote", {}) or {}
            greeks_block = row.get("greeks", {}) or {}
            day = row.get("day", {}) or {}
            try:
                exp = date.fromisoformat(details["expiration_date"])
                right = OptionRight.CALL if details["contract_type"] == "call" else OptionRight.PUT
            except (KeyError, ValueError):
                continue
            expirations.add(exp)
            contracts.append(
                OptionContract(
                    symbol=details.get("ticker", ""),
                    underlying=underlying.upper(),
                    right=right,
                    strike=Decimal(str(details["strike_price"])),
                    expiration=exp,
                    bid=Decimal(str(quote_block["bid"])) if quote_block.get("bid") is not None else None,
                    ask=Decimal(str(quote_block["ask"])) if quote_block.get("ask") is not None else None,
                    volume=day.get("volume"),
                    open_interest=row.get("open_interest"),
                    greeks=Greeks(
                        delta=greeks_block.get("delta"), gamma=greeks_block.get("gamma"),
                        theta=greeks_block.get("theta"), vega=greeks_block.get("vega"),
                        implied_volatility=row.get("implied_volatility"),
                    ),
                    as_of=now,
                    source=self.name,
                )
            )
        if not contracts:
            raise ProviderError(self.name, f"empty option chain for {underlying}")
        return OptionChain(
            underlying=underlying.upper(), expirations=sorted(expirations),
            contracts=contracts, as_of=now, source=self.name,
        )

    async def news(self, symbols: list[str] | None = None, *, limit: int = 25) -> list[NewsArticle]:
        params: dict[str, Any] = {"limit": min(limit, 100), "order": "desc", "sort": "published_utc"}
        if symbols:
            params["ticker"] = symbols[0].upper()
        payload = await self._get("/v2/reference/news", **params)
        articles: list[NewsArticle] = []
        for row in payload.get("results", []) or []:
            try:
                published = datetime.fromisoformat(row["published_utc"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            articles.append(
                NewsArticle(
                    headline=row.get("title", ""),
                    summary=row.get("description"),
                    url=row.get("article_url"),
                    symbols=[t.upper() for t in row.get("tickers", []) or []],
                    published_at=published,
                    source=f"{self.name}:{row.get('publisher', {}).get('name', 'unknown')}",
                )
            )
        return articles
