"""Tradier market data provider (https://documentation.tradier.com/brokerage-api/markets).

Capabilities: quotes, daily bars, option chains with greeks. Authentication:
Bearer token. Set ``options: {sandbox: true}`` to hit the sandbox host.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from ...core.enums import OptionRight
from ...core.errors import ProviderError
from ...core.models import Bar, Greeks, OptionChain, OptionContract, Quote
from ..base import DataCapability, MarketDataProvider, bar_end

_LIVE = "https://api.tradier.com/v1"
_SANDBOX = "https://sandbox.tradier.com/v1"


def _as_list(node: Any) -> list[dict[str, Any]]:
    """Tradier returns a dict for single results and a list for many."""
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


class TradierDataProvider(MarketDataProvider):
    name = "tradier_data"

    def __init__(self, *, api_key: str, timeout: float = 10.0,
                 options: dict[str, Any] | None = None) -> None:
        super().__init__(api_key=api_key, timeout=timeout, options=options)
        self._base = _SANDBOX if (options or {}).get("sandbox") else _LIVE
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.QUOTES, DataCapability.BARS, DataCapability.OPTIONS})

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._get_json(f"{self._base}{path}", params=params, headers=self._headers)

    async def quote(self, symbol: str) -> Quote:
        payload = await self._get("/markets/quotes", symbols=symbol.upper(), greeks="false")
        quotes = _as_list((payload.get("quotes") or {}).get("quote"))
        if not quotes:
            raise ProviderError(self.name, f"no quote for {symbol}")
        q = quotes[0]
        as_of = self._ts_from_epoch(q.get("trade_date"), millis=True)
        if as_of is None:
            raise ProviderError(self.name, f"quote for {symbol} has no timestamp")
        return Quote(
            symbol=symbol,
            bid=Decimal(str(q["bid"])) if q.get("bid") is not None else None,
            ask=Decimal(str(q["ask"])) if q.get("ask") is not None else None,
            last=Decimal(str(q["last"])) if q.get("last") is not None else None,
            bid_size=q.get("bidsize"), ask_size=q.get("asksize"),
            volume=q.get("volume"),
            as_of=as_of, source=self.name,
        )

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        if timeframe != "1d":
            raise ProviderError(self.name, "only 1d bars supported", retryable=False)
        payload = await self._get("/markets/history", symbol=symbol.upper(), interval="daily")
        days = _as_list((payload.get("history") or {}).get("day"))
        bars: list[Bar] = []
        for row in days[-limit:]:
            try:
                start = datetime.fromisoformat(row["date"]).replace(tzinfo=UTC)
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
                        low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
                        volume=int(row.get("volume", 0)),
                        start=start, end=bar_end(start, timeframe), source=self.name,
                    )
                )
            except (KeyError, ValueError):
                continue
        if not bars:
            raise ProviderError(self.name, f"no bars for {symbol} ({timeframe})")
        return bars

    async def option_chain(self, underlying: str, *, expiration: date | None = None) -> OptionChain:
        if expiration is None:
            exp_payload = await self._get(
                "/markets/options/expirations", symbol=underlying.upper(), includeAllRoots="true"
            )
            dates = _as_list((exp_payload.get("expirations") or {}).get("date"))
            if not dates:
                raise ProviderError(self.name, f"no option expirations for {underlying}")
            expiration = date.fromisoformat(str(dates[0]))
        payload = await self._get(
            "/markets/options/chains", symbol=underlying.upper(),
            expiration=expiration.isoformat(), greeks="true",
        )
        rows = _as_list((payload.get("options") or {}).get("option"))
        if not rows:
            raise ProviderError(self.name, f"empty option chain for {underlying} {expiration}")
        contracts: list[OptionContract] = []
        for row in rows:
            try:
                right = OptionRight.CALL if row["option_type"] == "call" else OptionRight.PUT
                greeks_block = row.get("greeks") or {}
                # Real quote time from the row's bid/ask/trade epochs (`or None`
                # guards Tradier's 0-for-missing values, which would parse to
                # 1970). greeks.updated_at is unusable — it is an ORATS batch
                # time (~hourly, naive ET) and would falsely grade live chains
                # stale; and receipt time would mask a frozen feed entirely.
                quote_times = [t for t in (
                    self._ts_from_epoch(row.get("bid_date") or None, millis=True),
                    self._ts_from_epoch(row.get("ask_date") or None, millis=True),
                    self._ts_from_epoch(row.get("trade_date") or None, millis=True),
                ) if t is not None]
                if not quote_times:
                    continue
                contracts.append(
                    OptionContract(
                        symbol=row["symbol"], underlying=underlying.upper(),
                        right=right,
                        strike=Decimal(str(row["strike"])),
                        expiration=date.fromisoformat(row["expiration_date"]),
                        bid=Decimal(str(row["bid"])) if row.get("bid") is not None else None,
                        ask=Decimal(str(row["ask"])) if row.get("ask") is not None else None,
                        last=Decimal(str(row["last"])) if row.get("last") is not None else None,
                        volume=row.get("volume"),
                        open_interest=row.get("open_interest"),
                        greeks=Greeks(
                            delta=greeks_block.get("delta"), gamma=greeks_block.get("gamma"),
                            theta=greeks_block.get("theta"), vega=greeks_block.get("vega"),
                            rho=greeks_block.get("rho"),
                            implied_volatility=greeks_block.get("mid_iv"),
                        ),
                        as_of=max(quote_times),
                        source=self.name,
                    )
                )
            except (KeyError, ValueError):
                continue
        if not contracts:
            raise ProviderError(
                self.name, f"option chain for {underlying} has no contracts with quote timestamps"
            )
        chain_as_of = min(c.as_of for c in contracts)
        return OptionChain(
            underlying=underlying.upper(), expirations=[expiration],
            contracts=contracts, as_of=chain_as_of, source=self.name,
        )
