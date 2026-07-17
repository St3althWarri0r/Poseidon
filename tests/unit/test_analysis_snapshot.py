# tests/unit/test_analysis_snapshot.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.ai.analysis.snapshot import Snapshot, build_snapshot
from poseidon.core.config import SnapshotConfig
from poseidon.core.models import Bar, InstrumentProfile, Quote

_AS_OF = datetime(2026, 7, 16, 15, 30, 2, tzinfo=UTC)


def _quote(last: str = "190.10") -> Quote:
    return Quote(symbol="AAPL", last=Decimal(last), as_of=_AS_OF, source="fake")


def _bars(n: int, *, start_close: str = "100.00") -> list[Bar]:
    out: list[Bar] = []
    base = Decimal(start_close)
    day0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        close = base + Decimal(i) * Decimal("0.25")
        start = day0 + timedelta(days=i)
        out.append(Bar(symbol="AAPL", open=close - Decimal("0.50"),
                       high=close + Decimal("1.00"), low=close - Decimal("1.00"),
                       close=close, volume=1000 + i, start=start,
                       end=start + timedelta(days=1), source="barsrc"))
    return out


def _profile() -> InstrumentProfile:
    return InstrumentProfile(symbol="AAPL", name="Apple Inc",
                             exchange="NASDAQ NMS - GLOBAL MARKET", currency="USD",
                             asset_type="equity", as_of=_AS_OF, source="finnhub")


class _Router:
    def __init__(self, quote: Quote | None = None, bars: list[Bar] | None = None,
                 profile: InstrumentProfile | None = None,
                 bars_raises: bool = False) -> None:
        self._quote = quote or _quote()
        self._bars = bars if bars is not None else []
        self._profile = profile
        self._bars_raises = bars_raises

    async def quote(self, symbol: str, allow_delayed: bool = True) -> Quote:
        return self._quote

    async def bars(self, symbol: str, timeframe: str = "1d", limit: int = 250) -> list[Bar]:
        if self._bars_raises:
            raise RuntimeError("bars down")
        return self._bars

    async def profile(self, symbol: str) -> InstrumentProfile | None:
        return self._profile


class _RouterNoProfile:
    async def quote(self, symbol: str, allow_delayed: bool = True) -> Quote:
        return _quote()

    async def bars(self, symbol: str, timeframe: str = "1d", limit: int = 250) -> list[Bar]:
        return []


async def test_snapshot_pins_price() -> None:
    snap = await build_snapshot(_Router(), "AAPL")
    assert isinstance(snap, Snapshot)
    assert "190.10" in snap.text and "AAPL" in snap.text
    assert snap.source == "fake"


async def test_snapshot_none_on_failure() -> None:
    class _Dead:
        async def quote(self, *a: object, **k: object) -> Quote:
            raise RuntimeError("no data")

        async def bars(self, *a: object, **k: object) -> list[Bar]:
            return []
    assert await build_snapshot(_Dead(), "AAPL") is None


async def test_snapshot_uses_quote_last_with_real_quote_model() -> None:
    # Regression: the old code read q.price, which core.models.Quote does not
    # have — against a real Quote the snapshot was never built.
    snap = await build_snapshot(_Router(quote=_quote("190.10")), "AAPL")
    assert snap is not None
    assert "last 190.10" in snap.text
    # Falls back to mid when last is missing.
    q = Quote(symbol="AAPL", bid=Decimal("189.00"), ask=Decimal("191.00"),
              as_of=_AS_OF, source="fake")
    snap = await build_snapshot(_Router(quote=q), "AAPL")
    assert snap is not None
    assert "last 190" in snap.text


async def test_renders_decimals_exactly() -> None:
    snap = await build_snapshot(
        _Router(quote=_quote("190.10"), bars=_bars(5)), "AAPL")
    assert snap is not None
    assert "190.10" in snap.text  # str(Decimal), not 190.1
    assert snap.payload is not None
    assert snap.payload["quote"]["last"] == "190.10"
    # Bar closes render verbatim too (two decimal places preserved).
    assert "101.00" in snap.text


async def test_latest_ohlcv_row_and_closes_oldest_first() -> None:
    bars = _bars(250)
    snap = await build_snapshot(_Router(bars=bars), "AAPL",
                                config=SnapshotConfig(closes_n=20))
    assert snap is not None
    last_bar = bars[-1]
    row = (f"latest daily bar {last_bar.start.date().isoformat()}: "
           f"O {last_bar.open} H {last_bar.high} L {last_bar.low} "
           f"C {last_bar.close} V {last_bar.volume} (source barsrc)")
    assert row in snap.text
    assert snap.payload is not None
    closes = snap.payload["closes"]
    assert closes["n"] == 20 and closes["oldest_first"] is True
    expected = [str(b.close) for b in bars[-20:]]
    assert closes["values"] == expected
    assert "last 20 closes (oldest first): " + ", ".join(expected) in snap.text
    lo = min(b.close for b in bars[-30:])
    hi = max(b.close for b in bars[-30:])
    assert f"30d close range {lo}-{hi}" in snap.text
    # Full 250-bar history: every fixed indicator resolves to a number.
    values = snap.text.split("never estimated): ", 1)[1].split("\nRules:")[0]
    assert "N/A" not in values


async def test_indicators_na_never_estimated() -> None:
    snap = await build_snapshot(_Router(bars=_bars(60)), "AAPL")
    assert snap is not None
    # 60 bars: SMA50 resolves, SMA200 cannot — must be N/A, never estimated.
    assert "SMA200 N/A (insufficient history)" in snap.text
    assert "SMA50 N/A" not in snap.text
    assert snap.payload is not None
    assert snap.payload["indicators"]["sma200"] == "N/A (insufficient history)"
    assert snap.payload["indicators"]["sma50"] != "N/A (insufficient history)"


async def test_survives_bars_failure() -> None:
    snap = await build_snapshot(_Router(bars_raises=True), "AAPL")
    assert snap is not None
    assert "last 190.10" in snap.text
    assert "N/A (bars unavailable)" in snap.text
    assert snap.payload is not None
    assert snap.payload["latest_bar"] is None
    assert snap.payload["range_30d"] is None
    assert snap.payload["closes"]["values"] == []
    assert all(v == "N/A (bars unavailable)"
               for v in snap.payload["indicators"].values())


async def test_identity_line_and_ticker_only_fail_open() -> None:
    snap = await build_snapshot(_Router(profile=_profile()), "AAPL")
    assert snap is not None
    assert ("identity: Apple Inc — exchange NASDAQ NMS - GLOBAL MARKET, type equity, "
            "currency USD (profile as_of " + _AS_OF.isoformat() +
            ", source finnhub)") in snap.text
    ticker_only = ("identity: unresolved — ticker AAPL only (no live profile); "
                   "do not infer the company from memory.")
    # Router resolves nothing → ticker-only.
    snap = await build_snapshot(_Router(profile=None), "AAPL")
    assert snap is not None and ticker_only in snap.text
    # Router without .profile at all → fail open to ticker-only.
    snap = await build_snapshot(_RouterNoProfile(), "AAPL")
    assert snap is not None and ticker_only in snap.text
    assert snap.payload is not None
    assert snap.payload["identity"]["resolved"] is False
    # identity: false skips the lookup entirely.
    class _Boom(_RouterNoProfile):
        async def profile(self, symbol: str) -> InstrumentProfile | None:
            raise AssertionError("profile must not be called when identity is off")
    snap = await build_snapshot(_Boom(), "AAPL",
                                config=SnapshotConfig(identity=False))
    assert snap is not None and ticker_only in snap.text


async def test_payload_structure_sources_and_note() -> None:
    snap = await build_snapshot(
        _Router(bars=_bars(250), profile=_profile()), "AAPL")
    assert snap is not None
    p = snap.payload
    assert p is not None
    assert set(p) == {"symbol", "identity", "quote", "latest_bar", "closes",
                      "range_30d", "indicators", "as_of", "sources", "note"}
    assert p["symbol"] == "AAPL"
    assert p["identity"] == {"name": "Apple Inc",
                             "exchange": "NASDAQ NMS - GLOBAL MARKET",
                             "asset_type": "equity", "currency": "USD",
                             "as_of": _AS_OF.isoformat(), "source": "finnhub"}
    assert p["quote"] == {"last": "190.10", "as_of": _AS_OF.isoformat(),
                          "source": "fake", "freshness": "real_time"}
    assert p["note"] == ("Source of truth for exact numbers this cycle. If another "
                         "tool result, news text, or recalled figure disagrees, flag "
                         "the discrepancy in your rationale/data_gaps — never average "
                         "or reconcile numbers yourself.")
    assert snap.sources == ("fake", "barsrc", "finnhub")
    assert p["sources"] == ["fake", "barsrc", "finnhub"]
    assert set(p["indicators"]) == {"sma50", "sma200", "ema10", "macd", "rsi14",
                                    "bollinger", "atr14"}
