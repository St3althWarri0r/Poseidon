"""Finnhub provider (https://finnhub.io/docs/api).

Capabilities: quotes, company & general news, earnings calendar, economic
calendar. Authentication: token query parameter.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ...core.errors import ProviderError
from ...core.models import EarningsEvent, EconomicEvent, NewsArticle, Quote
from ..base import DataCapability, MarketDataProvider

_BASE = "https://finnhub.io/api/v1"


class FinnhubProvider(MarketDataProvider):
    name = "finnhub"

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset(
            {
                DataCapability.QUOTES,
                DataCapability.NEWS,
                DataCapability.EARNINGS,
                DataCapability.ECONOMIC_CALENDAR,
                DataCapability.SECTOR,
            }
        )

    async def _get(self, path: str, **params: Any) -> Any:
        params["token"] = self._api_key
        return await self._get_json(f"{_BASE}{path}", params=params)

    async def quote(self, symbol: str) -> Quote:
        payload = await self._get("/quote", symbol=symbol.upper())
        current = payload.get("c")
        if current in (None, 0):
            raise ProviderError(self.name, f"no quote for {symbol}")
        as_of = self._ts_from_epoch(payload.get("t")) or self._now()
        return Quote(
            symbol=symbol,
            last=Decimal(str(current)),
            as_of=as_of,
            source=self.name,
        )

    async def sector(self, symbol: str) -> str:
        """GICS-style classification from the company profile (free tier).
        ETFs and unlisted symbols have no profile — that raises, it is not
        guessed."""
        payload = await self._get("/stock/profile2", symbol=symbol.upper())
        industry = (payload or {}).get("finnhubIndustry")
        if not industry:
            raise ProviderError(self.name, f"no sector classification for {symbol}",
                                retryable=False)
        return str(industry)

    async def news(self, symbols: list[str] | None = None, *, limit: int = 25) -> list[NewsArticle]:
        rows: list[dict[str, Any]]
        if symbols:
            today = datetime.now(UTC).date()
            rows = await self._get(
                "/company-news", symbol=symbols[0].upper(),
                **{"from": (today - timedelta(days=5)).isoformat(), "to": today.isoformat()},
            )
        else:
            rows = await self._get("/news", category="general")
        articles: list[NewsArticle] = []
        for row in (rows or [])[:limit]:
            published = self._ts_from_epoch(row.get("datetime"))
            if published is None:
                continue
            articles.append(
                NewsArticle(
                    headline=row.get("headline", ""),
                    summary=row.get("summary") or None,
                    url=row.get("url"),
                    symbols=[row["related"].upper()] if row.get("related") else [],
                    published_at=published,
                    source=f"{self.name}:{row.get('source', 'unknown')}",
                )
            )
        return articles

    async def earnings(self, *, days_ahead: int = 14,
                       symbols: list[str] | None = None) -> list[EarningsEvent]:
        today = datetime.now(UTC).date()
        payload = await self._get(
            "/calendar/earnings",
            **{"from": today.isoformat(), "to": (today + timedelta(days=days_ahead)).isoformat()},
        )
        wanted = {s.upper() for s in symbols} if symbols else None
        events: list[EarningsEvent] = []
        now = self._now()
        for row in payload.get("earningsCalendar", []) or []:
            sym = (row.get("symbol") or "").upper()
            if not sym or (wanted is not None and sym not in wanted):
                continue
            try:
                report = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            events.append(
                EarningsEvent(
                    symbol=sym,
                    report_date=report,
                    time_hint=row.get("hour") or None,
                    eps_estimate=row.get("epsEstimate"),
                    eps_actual=row.get("epsActual"),
                    revenue_estimate=row.get("revenueEstimate"),
                    revenue_actual=row.get("revenueActual"),
                    as_of=now,
                    source=self.name,
                )
            )
        return events

    async def economic_calendar(self, *, days_ahead: int = 7) -> list[EconomicEvent]:
        payload = await self._get("/calendar/economic")
        horizon = datetime.now(UTC) + timedelta(days=days_ahead)
        events: list[EconomicEvent] = []
        now = self._now()
        for row in payload.get("economicCalendar", []) or []:
            raw_time = row.get("time")
            if not raw_time:
                continue
            try:
                scheduled = datetime.fromisoformat(str(raw_time).replace(" ", "T"))
            except ValueError:
                continue
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=UTC)
            if scheduled > horizon:
                continue
            events.append(
                EconomicEvent(
                    name=row.get("event", ""),
                    country=row.get("country", ""),
                    scheduled_at=scheduled,
                    importance=str(row.get("impact")) if row.get("impact") is not None else None,
                    actual=str(row.get("actual")) if row.get("actual") is not None else None,
                    forecast=str(row.get("estimate")) if row.get("estimate") is not None else None,
                    previous=str(row.get("prev")) if row.get("prev") is not None else None,
                    as_of=now,
                    source=self.name,
                )
            )
        return events
