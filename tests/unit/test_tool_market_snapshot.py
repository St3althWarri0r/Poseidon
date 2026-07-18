# tests/unit/test_tool_market_snapshot.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from poseidon.ai.schemas import ALL_TOOLS, DATA_TOOLS
from poseidon.ai.tools import ToolDispatcher
from poseidon.core.config import SnapshotConfig
from poseidon.core.errors import DataError
from poseidon.core.models import Bar, InstrumentProfile, Quote

_AS_OF = datetime(2026, 7, 16, 15, 30, 2, tzinfo=UTC)


def _quote(last: str = "190.10") -> Quote:
    return Quote(symbol="AAPL", last=Decimal(last), as_of=_AS_OF, source="fake")


def _bars(n: int) -> list[Bar]:
    out: list[Bar] = []
    day0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        close = Decimal("100.00") + Decimal(i) * Decimal("0.25")
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
                 quote_raises: bool = False) -> None:
        self._quote = quote or _quote()
        self._bars = bars if bars is not None else []
        self._profile = profile
        self._quote_raises = quote_raises
        self.quote_calls: list[dict[str, Any]] = []

    async def quote(self, symbol: str, allow_delayed: bool = True) -> Quote:
        self.quote_calls.append({"symbol": symbol, "allow_delayed": allow_delayed})
        if self._quote_raises:
            raise RuntimeError("no data")
        return self._quote

    async def bars(self, symbol: str, timeframe: str = "1d", limit: int = 250) -> list[Bar]:
        return self._bars

    async def profile(self, symbol: str) -> InstrumentProfile | None:
        return self._profile


def _dispatcher(router: _Router, *, allow_delayed: bool = True,
                snapshot_config: SnapshotConfig | None = None) -> ToolDispatcher:
    return ToolDispatcher(router, None, None,  # type: ignore[arg-type]
                          allow_delayed_quotes=allow_delayed,
                          snapshot_config=snapshot_config)


async def test_tool_returns_payload_with_exact_price_strings() -> None:
    router = _Router(quote=_quote("190.10"), bars=_bars(250), profile=_profile())
    disp = _dispatcher(router)
    payload = await disp._tool_get_market_snapshot("AAPL")
    assert payload["symbol"] == "AAPL"
    assert payload["quote"]["last"] == "190.10"  # str(Decimal), never 190.1
    assert payload["identity"]["name"] == "Apple Inc"
    assert payload["latest_bar"]["close"] == "162.25"
    assert payload["closes"]["oldest_first"] is True
    assert all(isinstance(v, str) for v in payload["closes"]["values"])
    assert "never average or reconcile" in payload["note"]


async def test_tool_records_sources_used() -> None:
    router = _Router(quote=_quote(), bars=_bars(250), profile=_profile())
    disp = _dispatcher(router)
    await disp._tool_get_market_snapshot("AAPL")
    assert disp.sources_used == {"fake", "barsrc", "finnhub"}


async def test_tool_raises_data_error_without_quote() -> None:
    disp = _dispatcher(_Router(quote_raises=True))
    with pytest.raises(DataError, match="no live snapshot available for AAPL"):
        await disp._tool_get_market_snapshot("AAPL")


async def test_tool_respects_allow_delayed() -> None:
    for allow in (True, False):
        router = _Router()
        disp = _dispatcher(router, allow_delayed=allow)
        await disp._tool_get_market_snapshot("AAPL")
        assert router.quote_calls == [{"symbol": "AAPL", "allow_delayed": allow}]


async def test_schema_in_data_tools_and_all_tools_with_source_of_truth_footer() -> None:
    for tools in (DATA_TOOLS, ALL_TOOLS):
        matches = [t for t in tools if t["name"] == "get_market_snapshot"]
        assert len(matches) == 1
    schema = matches[0]
    assert "source of truth" in schema["description"]
    assert "never reconcile" in schema["description"]
    assert schema["input_schema"]["required"] == ["symbol"]
    assert schema["input_schema"]["properties"] == {"symbol": {"type": "string"}}
    assert schema["input_schema"]["additionalProperties"] is False


async def test_dispatcher_threads_non_default_snapshot_config() -> None:
    # A non-default SnapshotConfig must reach build_snapshot through the dispatcher:
    # closes_n=5 (vs the default 20) surfaces as closes.n == 5. A mutant that
    # ignored self._snapshot_config would emit the default 20 and fail here.
    router = _Router(quote=_quote("190.10"), bars=_bars(250), profile=_profile())
    disp = _dispatcher(router, snapshot_config=SnapshotConfig(closes_n=5))
    payload = await disp._tool_get_market_snapshot("AAPL")
    assert payload["closes"]["n"] == 5
    assert len(payload["closes"]["values"]) == 5
    # Exactly the last five closes, oldest first, rendered verbatim (str(Decimal)).
    expected = [str(b.close) for b in _bars(250)[-5:]]
    assert payload["closes"]["values"] == expected
