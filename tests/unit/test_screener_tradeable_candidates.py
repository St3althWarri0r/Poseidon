"""The screener must not nominate candidates the PM cannot evaluate.

Ranking uses 20-day median dollar volume from daily bars — a HISTORICAL
liquidity measure. Nothing checked whether a symbol is quotable *now*. So the
screener happily nominated pairs that had not printed in half an hour, the PM
tried to quote them, the freshness gate refused, and the cycle was spent on a
`data_gaps` entry instead of a decision.

Measured on the operator's live crypto universe (2026-08-14): median staleness
37.6s, p75 230s, p90 413s, **max 1920s (MANA/USD — 32 minutes)**. Their logs show
cycle after cycle ending with `"AAVE/USD quote unavailable"`.

The screener and the freshness policy were disagreeing about what "tradeable"
means. The fix is to make the screener ask the *same* question the PM will:
`router.quote(symbol, allow_delayed=False)`. A candidate that cannot answer it is
dropped and backfilled from the next-ranked, so the PM always receives a full
slate of actionable names.

Two properties matter as much as the filtering:

* **bounded** — it checks the top of the ranking, never the whole universe;
* **fail-open** — if the liveness probe itself is broken (provider outage), the
  unfiltered ranking is returned rather than an empty slate. Degrading to the
  old behaviour beats handing the PM nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poseidon.core.errors import StaleDataError
from poseidon.core.models import Quote
from poseidon.strategy.screener import MarketScreener, ScoredCandidate


class _Router:
    """Quotes only the symbols named live; everything else is stale."""

    def __init__(self, live: set[str], *, explode: bool = False) -> None:
        self.live = live
        self.explode = explode
        self.asked: list[str] = []

    async def quote(self, symbol: str, *, allow_delayed: bool = False) -> Quote:
        self.asked.append(symbol)
        if self.explode:
            raise RuntimeError("router is broken")
        if symbol not in self.live:
            raise StaleDataError(f"{symbol} quote is stale")
        return Quote(symbol=symbol, bid=Decimal("1"), ask=Decimal("2"),
                     last=Decimal("1.5"), as_of=datetime.now(UTC), source="fake")


def _ranked(*symbols: str) -> list[ScoredCandidate]:
    return [ScoredCandidate(s, score=1.0 - i / 100, dollar_volume=5e6, r_1m=0.1, r_3m=0.2)
            for i, s in enumerate(symbols)]


def _screener(router: object, top_n: int = 3) -> MarketScreener:
    from poseidon.core.config import CryptoScreenerConfig
    cfg = CryptoScreenerConfig(enabled=True, top_n=top_n)
    return MarketScreener(cfg, router)  # type: ignore[arg-type]


async def test_unquotable_candidates_are_dropped() -> None:
    router = _Router(live={"BTC/USD", "ETH/USD", "SOL/USD"})
    kept = await _screener(router).filter_tradeable(
        _ranked("MANA/USD", "BTC/USD", "LDO/USD", "ETH/USD", "SOL/USD"))
    assert [c.symbol for c in kept] == ["BTC/USD", "ETH/USD", "SOL/USD"]


async def test_ranking_order_is_preserved() -> None:
    router = _Router(live={"BTC/USD", "ETH/USD", "SOL/USD"})
    kept = await _screener(router).filter_tradeable(
        _ranked("SOL/USD", "MANA/USD", "BTC/USD", "ETH/USD"))
    assert [c.symbol for c in kept] == ["SOL/USD", "BTC/USD", "ETH/USD"]


async def test_it_backfills_to_a_full_slate() -> None:
    """Dropping a dead name must not shrink what the PM gets to look at."""
    router = _Router(live={"C/USD", "D/USD", "E/USD"})
    kept = await _screener(router, top_n=3).filter_tradeable(
        _ranked("A/USD", "B/USD", "C/USD", "D/USD", "E/USD"))
    assert len(kept) == 3


async def test_a_broken_probe_fails_open_to_the_unfiltered_ranking() -> None:
    """A provider outage must not empty the candidate slate — that would turn a
    data problem into 'the PM has nothing to consider'."""
    ranked = _ranked("A/USD", "B/USD", "C/USD")
    kept = await _screener(_Router(live=set(), explode=True)).filter_tradeable(ranked)
    assert [c.symbol for c in kept] == ["A/USD", "B/USD", "C/USD"]


async def test_every_candidate_unquotable_falls_back_to_the_ranking() -> None:
    """Originally this asserted an empty slate — "the probe worked, nothing is
    live, so hand over nothing". The existing screener suites then failed with
    `selected=0` across the board, which is the point: a router that cannot
    quote blanks EVERYTHING. Total elimination is far likelier to mean the probe
    or the freshness window is misconfigured than that nothing in the S&P 500
    trades, so it falls back to the unfiltered ranking and warns. The PM's own
    gate still refuses anything genuinely stale."""
    ranked = _ranked("A/USD", "B/USD")
    kept = await _screener(_Router(live=set())).filter_tradeable(ranked)
    assert [c.symbol for c in kept] == ["A/USD", "B/USD"]


async def test_the_scan_is_bounded() -> None:
    """It must not quote the entire universe to fill a slate of 3."""
    router = _Router(live={"Z/USD"})
    ranked = _ranked(*[f"S{i}/USD" for i in range(200)], "Z/USD")
    await _screener(router, top_n=3).filter_tradeable(ranked)
    assert len(router.asked) <= 30, f"probed {len(router.asked)} symbols — unbounded"


async def test_an_already_short_ranking_is_handled() -> None:
    router = _Router(live={"A/USD"})
    kept = await _screener(router, top_n=10).filter_tradeable(_ranked("A/USD"))
    assert [c.symbol for c in kept] == ["A/USD"]


@pytest.mark.parametrize("ranked", [[], _ranked()])
async def test_empty_input_is_empty_output(ranked: list) -> None:
    router = _Router(live=set())
    assert await _screener(router).filter_tradeable(ranked) == []
    assert router.asked == []
