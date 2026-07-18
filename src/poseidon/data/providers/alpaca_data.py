"""Alpaca Market Data provider (https://docs.alpaca.markets/docs/about-market-data-api).

Capabilities: quotes, bars, news, option chain snapshots. Authentication:
APCA-API-KEY-ID / APCA-API-SECRET-KEY headers. The vault credential for
this provider is a JSON object: {"key_id": "...", "secret_key": "..."} —
pass it via ``options`` when constructing (see registry wiring).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ...core.enums import OptionRight
from ...core.errors import ProviderError
from ...core.models import Bar, Greeks, NewsArticle, OptionChain, OptionContract, Quote
from ...core.symbols import is_crypto_symbol, normalize_crypto_symbol
from ..base import DataCapability, MarketDataProvider, bar_end

_DATA_BASE = "https://data.alpaca.markets"

_TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day", "1w": "1Week"}


def _parse_occ(symbol: str) -> tuple[str, date, OptionRight, Decimal] | None:
    """Parse an OCC option symbol like AAPL240621C00190000."""
    try:
        for i, ch in enumerate(symbol):
            if ch.isdigit():
                root, rest = symbol[:i], symbol[i:]
                break
        else:
            return None
        exp = datetime.strptime(rest[:6], "%y%m%d").date()
        right = OptionRight.CALL if rest[6] == "C" else OptionRight.PUT
        strike = Decimal(rest[7:]) / 1000
        return root, exp, right, strike
    except (ValueError, IndexError):
        return None


class AlpacaDataProvider(MarketDataProvider):
    name = "alpaca"

    def __init__(self, *, api_key: str, timeout: float = 10.0,
                 options: dict[str, Any] | None = None) -> None:
        super().__init__(api_key=api_key, timeout=timeout, options=options)
        # api_key holds key_id; secret comes through options (wired from vault JSON).
        secret = (options or {}).get("secret_key", "")
        if not secret:
            raise ProviderError(self.name, "credential must include secret_key", retryable=False)
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}

    def capabilities(self) -> frozenset[DataCapability]:
        # Alpaca serves spot crypto (v1beta3) free with the same alpaca_keys, so
        # CRYPTO is advertised unconditionally alongside the equity capabilities.
        return frozenset(
            {DataCapability.QUOTES, DataCapability.BARS, DataCapability.OPTIONS,
             DataCapability.NEWS, DataCapability.CRYPTO}
        )

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._get_json(f"{_DATA_BASE}{path}", params=params, headers=self._headers)

    def _bar_from_row(self, symbol: str, row: dict[str, Any], timeframe: str,
                      *, crypto: bool = False) -> Bar | None:
        """Parse one Alpaca bar row into a :class:`Bar`, or ``None`` if the row
        is malformed. Single source of truth for the single-symbol, crypto, and
        batched bar paths. Crypto volume is fractional coins, so it is floored
        via ``int(float(...))``; equity volume is already an integer share count."""
        try:
            start = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
            volume = int(float(row.get("v", 0))) if crypto else int(row.get("v", 0))
            return Bar(
                symbol=symbol,
                open=Decimal(str(row["o"])), high=Decimal(str(row["h"])),
                low=Decimal(str(row["l"])), close=Decimal(str(row["c"])),
                volume=volume,
                start=start, end=bar_end(start, timeframe), source=self.name,
            )
        except (KeyError, ValueError):
            return None

    async def quote(self, symbol: str) -> Quote:
        if is_crypto_symbol(symbol):
            return await self._crypto_quote(symbol)
        payload = await self._get(f"/v2/stocks/{symbol.upper()}/quotes/latest")
        q = payload.get("quote")
        if not q:
            raise ProviderError(self.name, f"no quote for {symbol}")
        if not q.get("t"):
            raise ProviderError(self.name, f"quote for {symbol} has no timestamp")
        as_of = datetime.fromisoformat(q["t"].replace("Z", "+00:00"))
        return Quote(
            symbol=symbol,
            bid=Decimal(str(q["bp"])) if q.get("bp") else None,
            ask=Decimal(str(q["ap"])) if q.get("ap") else None,
            bid_size=q.get("bs"), ask_size=q.get("as"),
            as_of=as_of, source=self.name,
        )

    async def _crypto_quote(self, symbol: str) -> Quote:
        # Crypto data API (v1beta3) is multi-symbol and keyed by symbol:
        #   GET /v1beta3/crypto/us/latest/quotes?symbols=BTC/USD
        #   -> {"quotes": {"BTC/USD": {"bp":.., "ap":.., "bs":.., "as":.., "t":".."}}}
        sym = normalize_crypto_symbol(symbol)
        payload = await self._get("/v1beta3/crypto/us/latest/quotes", symbols=sym)
        q = (payload.get("quotes") or {}).get(sym)
        if not q:
            raise ProviderError(self.name, f"no quote for {sym}")
        if not q.get("t"):
            raise ProviderError(self.name, f"quote for {sym} has no timestamp")
        as_of = datetime.fromisoformat(q["t"].replace("Z", "+00:00"))
        # bid_size/ask_size (Quote fields) are integer share/contract counts;
        # crypto book sizes are fractional coins (e.g. 0.5 BTC) that cannot be an
        # int without lying, and no downstream rule needs them (SpreadRule uses
        # bid/ask) — so leave them unset for crypto.
        return Quote(
            symbol=sym,
            bid=Decimal(str(q["bp"])) if q.get("bp") else None,
            ask=Decimal(str(q["ap"])) if q.get("ap") else None,
            as_of=as_of, source=self.name,
        )

    async def bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        if is_crypto_symbol(symbol):
            return await self._crypto_bars(symbol, timeframe=timeframe, limit=limit)
        tf = _TIMEFRAMES.get(timeframe)
        if tf is None:
            raise ProviderError(self.name, f"unsupported timeframe {timeframe}", retryable=False)
        # Alpaca defaults `start` to the beginning of the CURRENT day (0-1 daily
        # bars, none on weekends) — request a real lookback window, newest first.
        lookback = 730 if timeframe in ("1d", "1w") else 30
        start_date = (datetime.now(UTC) - timedelta(days=lookback)).date().isoformat()
        payload = await self._get(
            f"/v2/stocks/{symbol.upper()}/bars", timeframe=tf, limit=min(limit, 10000),
            adjustment="split", feed=self._options.get("feed", "iex"),
            start=start_date, sort="desc",
        )
        bars: list[Bar] = []
        for row in payload.get("bars", []) or []:
            bar = self._bar_from_row(symbol.upper(), row, timeframe)
            if bar is not None:
                bars.append(bar)
        bars.reverse()  # requested newest-first; consumers expect chronological
        if not bars:
            raise ProviderError(self.name, f"no bars for {symbol} ({timeframe})")
        return bars

    async def bars_multi(self, symbols: list[str], *, timeframe: str,
                         limit: int) -> dict[str, list[Bar]]:
        # Batched equity daily bars: GET /v2/stocks/bars?symbols=A,B,..&... — one
        # (paginated) round-trip for the whole screener universe instead of N.
        # Chunk to `max_batch_symbols` symbols/request and follow next_page_token
        # until exhausted, merging each `bars[SYM]` list. A chunk that errors
        # drops only its own symbols (best-effort); the crypto multi-bars path
        # differs (own endpoint), so crypto symbols are excluded here.
        tf = _TIMEFRAMES.get(timeframe)
        if tf is None:
            raise ProviderError(self.name, f"unsupported timeframe {timeframe}", retryable=False)
        # Lookback scaled to the requested bar count (plus slack for weekends/
        # holidays) so a batch never over-fetches years of history per symbol.
        if timeframe == "1w":
            lookback = limit * 7 + 30
        elif timeframe == "1d":
            lookback = limit * 2 + 15
        else:
            lookback = 30
        start_date = (datetime.now(UTC) - timedelta(days=lookback)).date().isoformat()
        chunk_size = max(1, int(self._options.get("max_batch_symbols", 200)))
        equities = [s.upper() for s in symbols if not is_crypto_symbol(s)]

        rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(equities), chunk_size):
            chunk = equities[i:i + chunk_size]
            try:
                await self._fetch_bars_chunk(chunk, tf, start_date, limit, rows_by_symbol)
            except ProviderError:
                continue  # best-effort: a failed chunk drops only its own symbols

        result: dict[str, list[Bar]] = {}
        for sym, rows in rows_by_symbol.items():
            parsed = [b for b in (self._bar_from_row(sym, r, timeframe) for r in rows)
                      if b is not None]
            parsed.reverse()  # requested newest-first; consumers expect chronological
            if parsed:
                result[sym] = parsed[-limit:]  # cap to the requested window per symbol
        return result

    async def _fetch_bars_chunk(self, chunk: list[str], tf: str, start_date: str,
                                limit: int, rows_by_symbol: dict[str, list[dict[str, Any]]]) -> None:
        """Fetch one symbol chunk, following ``next_page_token`` to exhaustion and
        accumulating each page's per-symbol rows (newest-first) into
        ``rows_by_symbol``."""
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                # Multi-symbol /v2/stocks/bars applies `limit` to the TOTAL bars
                # across ALL symbols in a page (hence next_page_token), NOT
                # per-symbol — so send the endpoint MAX, not the caller's small
                # per-symbol bars_limit, or a ~200-symbol chunk paginates ~100x
                # (spec §8 budget: 4-8 req/screen). The `start` window bounds the
                # data and the final parsed[-limit:] trims per symbol.
                "symbols": ",".join(chunk), "timeframe": tf, "limit": 10000,
                "adjustment": "split", "feed": self._options.get("feed", "iex"),
                "start": start_date, "sort": "desc",
            }
            if page_token:
                params["page_token"] = page_token
            payload = await self._get("/v2/stocks/bars", **params)
            for sym, rows in (payload.get("bars") or {}).items():
                rows_by_symbol.setdefault(sym, []).extend(rows or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                break

    async def _crypto_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        # Crypto bars (v1beta3) are multi-symbol and keyed by symbol; no split
        # adjustment and no equity `feed` (the "us" feed is in the path):
        #   GET /v1beta3/crypto/us/bars?symbols=BTC/USD&timeframe=1Day&start=..
        #   -> {"bars": {"BTC/USD": [{"o","h","l","c","v","t"}, ...]}}
        tf = _TIMEFRAMES.get(timeframe)
        if tf is None:
            raise ProviderError(self.name, f"unsupported timeframe {timeframe}", retryable=False)
        sym = normalize_crypto_symbol(symbol)
        lookback = 730 if timeframe in ("1d", "1w") else 30
        start_date = (datetime.now(UTC) - timedelta(days=lookback)).date().isoformat()
        payload = await self._get(
            "/v1beta3/crypto/us/bars", symbols=sym, timeframe=tf,
            limit=min(limit, 10000), start=start_date, sort="desc",
        )
        rows = (payload.get("bars") or {}).get(sym) or []
        bars: list[Bar] = []
        for row in rows:
            # crypto volume is fractional coins; Bar.volume is an int coin count
            # (VolumeRule is exempt for crypto, §E).
            bar = self._bar_from_row(sym, row, timeframe, crypto=True)
            if bar is not None:
                bars.append(bar)
        bars.reverse()  # requested newest-first; consumers expect chronological
        if not bars:
            raise ProviderError(self.name, f"no bars for {sym} ({timeframe})")
        return bars

    async def option_chain(self, underlying: str, *, expiration: date | None = None) -> OptionChain:
        params: dict[str, Any] = {"feed": self._options.get("options_feed", "indicative"), "limit": 500}
        if expiration:
            params["expiration_date"] = expiration.isoformat()
        payload = await self._get(f"/v1beta1/options/snapshots/{underlying.upper()}", **params)
        snapshots = payload.get("snapshots") or {}
        contracts: list[OptionContract] = []
        expirations: set[date] = set()
        for occ_symbol, snap in snapshots.items():
            parsed = _parse_occ(occ_symbol)
            if parsed is None:
                continue
            _, exp, right, strike = parsed
            quote_block = snap.get("latestQuote") or {}
            # Real per-contract quote time (indicative feeds freeze when the
            # market is closed); never stamp receipt time — a fabricated
            # as_of would grade a frozen chain REAL_TIME at the router.
            ts = self._parse_ts(quote_block.get("t"))
            if ts is None:
                continue
            expirations.add(exp)
            greeks_block = snap.get("greeks") or {}
            contracts.append(
                OptionContract(
                    symbol=occ_symbol, underlying=underlying.upper(),
                    right=right, strike=strike, expiration=exp,
                    bid=Decimal(str(quote_block["bp"])) if quote_block.get("bp") else None,
                    ask=Decimal(str(quote_block["ap"])) if quote_block.get("ap") else None,
                    greeks=Greeks(
                        delta=greeks_block.get("delta"), gamma=greeks_block.get("gamma"),
                        theta=greeks_block.get("theta"), vega=greeks_block.get("vega"),
                        rho=greeks_block.get("rho"),
                        implied_volatility=snap.get("impliedVolatility"),
                    ),
                    as_of=ts, source=self.name,
                )
            )
        if not contracts:
            raise ProviderError(
                self.name, f"option chain for {underlying} has no contracts with quote timestamps"
            )
        chain_as_of = min(c.as_of for c in contracts)
        return OptionChain(
            underlying=underlying.upper(), expirations=sorted(expirations),
            contracts=contracts, as_of=chain_as_of, source=self.name,
        )

    @staticmethod
    def _parse_ts(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def news(self, symbols: list[str] | None = None, *, limit: int = 25) -> list[NewsArticle]:
        params: dict[str, Any] = {"limit": min(limit, 50), "sort": "desc"}
        if symbols:
            params["symbols"] = ",".join(s.upper() for s in symbols[:10])
        payload = await self._get("/v1beta1/news", **params)
        articles: list[NewsArticle] = []
        for row in payload.get("news", []) or []:
            try:
                published = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            articles.append(
                NewsArticle(
                    headline=row.get("headline", ""),
                    summary=row.get("summary") or None,
                    url=row.get("url"),
                    symbols=[s.upper() for s in row.get("symbols", []) or []],
                    published_at=published,
                    source=f"{self.name}:{row.get('source', 'unknown')}",
                )
            )
        return articles
