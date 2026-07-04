"""Twelve Data provider (https://twelvedata.com/docs).

Capabilities: quotes and time-series bars. Authentication: apikey query
parameter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ...core.errors import ProviderError, ProviderRateLimitError
from ...core.models import Bar, Quote
from ..base import DataCapability, MarketDataProvider, bar_end

_BASE = "https://api.twelvedata.com"

_TIMEFRAMES = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "1d": "1day", "1w": "1week"}


class TwelveDataProvider(MarketDataProvider):
    name = "twelvedata"

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.QUOTES, DataCapability.BARS})

    async def _get(self, path: str, **params: Any) -> Any:
        params["apikey"] = self._api_key
        payload = await self._get_json(f"{_BASE}{path}", params=params)
        # Twelve Data reports errors in-band with HTTP 200.
        if isinstance(payload, dict) and payload.get("status") == "error":
            code = payload.get("code")
            message = payload.get("message", "unknown error")
            if code == 429:
                raise ProviderRateLimitError(self.name)
            raise ProviderError(self.name, f"{code}: {message}")
        return payload

    async def quote(self, symbol: str) -> Quote:
        payload = await self._get("/quote", symbol=symbol.upper())
        ts = payload.get("timestamp")
        as_of = self._ts_from_epoch(ts)
        if as_of is None:
            # No provider timestamp: refuse rather than stamp now() and let a
            # quote of unknown age pass the freshness gate (no fabricated age).
            raise ProviderError(self.name, f"quote for {symbol} has no timestamp")
        close = payload.get("close")
        if close is None:
            raise ProviderError(self.name, f"no quote for {symbol}")
        return Quote(
            symbol=symbol,
            last=Decimal(str(close)),
            volume=int(float(payload["volume"])) if payload.get("volume") else None,
            as_of=as_of,
            source=self.name,
        )

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        interval = _TIMEFRAMES.get(timeframe)
        if interval is None:
            raise ProviderError(self.name, f"unsupported timeframe {timeframe}", retryable=False)
        payload = await self._get(
            "/time_series", symbol=symbol.upper(), interval=interval,
            outputsize=min(limit, 5000), timezone="UTC",
        )
        bars: list[Bar] = []
        for row in payload.get("values", []) or []:
            try:
                start = datetime.fromisoformat(row["datetime"]).replace(tzinfo=UTC)
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        open=Decimal(row["open"]), high=Decimal(row["high"]),
                        low=Decimal(row["low"]), close=Decimal(row["close"]),
                        volume=int(float(row.get("volume", 0) or 0)),
                        start=start, end=bar_end(start, timeframe), source=self.name,
                    )
                )
            except (KeyError, ValueError):
                continue
        bars.reverse()
        if not bars:
            raise ProviderError(self.name, f"no bars for {symbol} ({timeframe})")
        return bars
