"""A malformed symbol from the model must die at the tool boundary.

Three failures observed live on 2026-08-14, all from one missing guard:

1. **Bars never learned the symbol shape.** `get_quote` was taught to name the
   right form; `get_bars` and `get_market_snapshot` were not. So::

       get_bars("BTCUSD")  -> all providers failed for 'bars':
                              [coinbase] coinbase serves crypto pairs only, not 'BTCUSD'
                              [alpaca] no bars for BTCUSD (1d) ...

   and the cycle recorded ``data_gaps: ["BTCUSD bars unavailable", "ETH/USD
   bars unavailable", ...]`` for six pairs and traded nothing. The data exists;
   only the shape was wrong, and nothing told the model so.

2. **An EMPTY symbol reached every provider.** The model emitted
   ``{"symbol": ""}`` and the dispatcher forwarded it::

       [alpaca] HTTP 400: parameter 'symbol' is empty, can't bind its value
       [finnhub] no quote for
       [alphavantage] rate limited

3. ...and (2) is the serious one, because `DataRouter._route` treats those as
   PROVIDER failures: `record_failure` puts the slot in the penalty box with
   exponential backoff (`_PENALTY_BASE` 15s, doubling, capped at 600s). A
   garbage argument from the model therefore degrades live data routing for
   every *legitimate* symbol that follows. Model output must never be able to
   penalize a healthy provider.

The same corruption family also produced a symbol of literal garbage
(``AAVE?1????..??..??..…``), which was likewise sent upstream as a URL
parameter.

So the property under test is not "raises a helpful error" — it is
**rejected input never reaches a provider at all**. An error-message assertion
alone would still pass while the providers were being hit and penalized first,
which is the actual defect.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from poseidon.ai.tools import ToolDispatcher
from poseidon.core.models import Bar, NewsArticle, Quote

# The exact corrupted symbol seen in the log, truncated to its shape.
GARBAGE = "AAVE?1????..??..??..??..??..??..??..…??…………?"


class _SpyRouter:
    """Records every data call. The assertion is that it records NOTHING."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def quote(self, symbol: str, allow_delayed: bool = True) -> Quote:
        self.calls.append(("quote", symbol))
        return Quote(symbol=symbol, bid=Decimal("1"), ask=Decimal("2"),
                     last=Decimal("1.5"), as_of=datetime.now(UTC), source="spy")

    async def bars(self, symbol: str, timeframe: str = "1d",
                   limit: int = 100) -> list[Bar]:
        self.calls.append(("bars", symbol))
        now = datetime.now(UTC)
        return [Bar(symbol=symbol.upper(), open=Decimal("1"), high=Decimal("2"),
                    low=Decimal("1"), close=Decimal("1.5"), volume=10,
                    start=now, end=now, source="spy")]

    async def profile(self, symbol: str) -> None:
        self.calls.append(("profile", symbol))

    async def option_chain(self, underlying: str, expiration: object = None,
                           allow_delayed: bool = True) -> list[Any]:
        self.calls.append(("option_chain", underlying))
        return []

    async def news(self, symbols: list[str] | None, limit: int = 10) -> list[NewsArticle]:
        self.calls.append(("news", symbols))
        return []


def _dispatcher(router: _SpyRouter) -> ToolDispatcher:
    return ToolDispatcher(router, None, None,  # type: ignore[arg-type]
                          allow_delayed_quotes=True)


# Every single-symbol tool, with the rest of its required arguments.
SINGLE_SYMBOL_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("get_quote", {}),
    ("get_bars", {"timeframe": "1d", "limit": 10}),
    ("get_market_snapshot", {}),
]

BAD_SYMBOLS = ["", "   ", GARBAGE, "A" * 64]


@pytest.mark.parametrize("tool,extra", SINGLE_SYMBOL_TOOLS)
@pytest.mark.parametrize("bad", BAD_SYMBOLS)
async def test_a_malformed_symbol_never_reaches_a_provider(
    tool: str, extra: dict[str, Any], bad: str
) -> None:
    """THE severity-1 property: no provider call, so no penalty box."""
    router = _SpyRouter()
    payload, is_error = await _dispatcher(router).dispatch(tool, {"symbol": bad, **extra})
    assert is_error, f"{tool} accepted {bad!r}"
    assert router.calls == [], (
        f"{tool}({bad!r}) reached the data layer: {router.calls}. "
        "DataRouter.record_failure would penalize those providers with "
        "exponential backoff, degrading routing for real symbols."
    )
    assert "error" in json.loads(payload)


async def test_an_empty_underlying_never_reaches_the_option_chain() -> None:
    router = _SpyRouter()
    _, is_error = await _dispatcher(router).dispatch(
        "get_option_chain", {"underlying": "", "expiration": None})
    assert is_error
    assert router.calls == []


async def test_an_empty_symbol_in_a_news_list_never_reaches_a_provider() -> None:
    router = _SpyRouter()
    _, is_error = await _dispatcher(router).dispatch(
        "get_news", {"symbols": ["AAPL", ""], "limit": 5})
    assert is_error
    assert router.calls == []


# -- shape guidance: the data exists, only the form was wrong ---------------------

async def test_bars_canonicalizes_the_slashless_crypto_form() -> None:
    """``BTCUSD`` -> ``BTC/USD`` is UNAMBIGUOUS (the base is a known crypto
    base), so it is fixed rather than merely reported — this is the live
    "BTCUSD bars unavailable" gap."""
    router = _SpyRouter()
    payload, is_error = await _dispatcher(router).dispatch(
        "get_bars", {"symbol": "BTCUSD", "timeframe": "1d", "limit": 10})
    assert not is_error, payload
    assert router.calls == [("bars", "BTC/USD")]


async def test_bars_teaches_the_form_for_a_bare_crypto_base() -> None:
    """``ADA`` is AMBIGUOUS (an equity could share the name), so it is NOT
    rewritten — the error names the right form instead."""
    router = _SpyRouter()
    payload, is_error = await _dispatcher(router).dispatch(
        "get_bars", {"symbol": "ADA", "timeframe": "1d", "limit": 10})
    assert is_error
    assert "ADA/USD" in json.loads(payload)["error"]
    assert router.calls == []


async def test_the_snapshot_tool_teaches_the_form_too() -> None:
    """Live: ``no live snapshot available for BTC`` — true, useless, and the
    actionable hint was thrown away."""
    router = _SpyRouter()
    payload, is_error = await _dispatcher(router).dispatch(
        "get_market_snapshot", {"symbol": "BTC"})
    assert is_error
    assert "BTC/USD" in json.loads(payload)["error"]
    assert router.calls == []


# -- the common path must be untouched --------------------------------------------

@pytest.mark.parametrize("good", ["AAPL", "ADA/USD", "BRK.B", "BF-B", "SPY"])
async def test_a_valid_symbol_is_passed_through_unchanged(good: str) -> None:
    """Punctuated equity tickers are real (BRK.B, BF-B) and must not be
    mangled by crypto canonicalization."""
    router = _SpyRouter()
    _, is_error = await _dispatcher(router).dispatch("get_quote", {"symbol": good})
    assert not is_error
    assert router.calls == [("quote", good)]


async def test_a_lowercase_symbol_is_normalized_not_rejected() -> None:
    router = _SpyRouter()
    _, is_error = await _dispatcher(router).dispatch("get_quote", {"symbol": "ada/usd"})
    assert not is_error
    assert router.calls == [("quote", "ADA/USD")]


async def test_a_valid_news_list_still_works() -> None:
    router = _SpyRouter()
    _, is_error = await _dispatcher(router).dispatch(
        "get_news", {"symbols": ["AAPL", "MSFT"], "limit": 5})
    assert not is_error
    assert router.calls == [("news", ["AAPL", "MSFT"])]
