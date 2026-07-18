"""Batched daily bars (screener TASK 3): provider ``bars_multi`` default,
``DataRouter.bars_multi`` (select → hygiene → single-symbol degrade), and the
Alpaca multi-symbol parse+pagination. No network — the Alpaca test drives an
``httpx.MockTransport`` so the real ``_get`` runs against canned pages.

The batch path is throughput plumbing for the screener: it picks WHICH symbols
the AI evaluates and never trades. It degrades to single-symbol bars when no
provider implements the batch method, and applies the same boundary hygiene as
``DataRouter.bars`` (drop unsound bars; drop a frozen-feed symbol) so a glitchy
feed can never skew the ranking downstream.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from poseidon.core.clock import FreshnessPolicy
from poseidon.data.providers.alpaca_data import AlpacaDataProvider
from poseidon.data.router import DataRouter

from ..conftest import FakeBatchProvider


@pytest.fixture
def policy() -> FreshnessPolicy:
    return FreshnessPolicy(real_time_max_age=5.0, delayed_max_age=900.0)


async def test_router_bars_multi_returns_dict(policy: FreshnessPolicy) -> None:
    provider = FakeBatchProvider(name="batch", bars_count=90)
    router = DataRouter([(provider, 10)], policy)

    result = await router.bars_multi(["AAPL", "MSFT", "NVDA"], timeframe="1d", limit=90)

    assert set(result) == {"AAPL", "MSFT", "NVDA"}
    assert all(len(bars) == 90 for bars in result.values())
    assert provider.multi_calls == 1
    assert provider.single_calls == 0  # batch path used, not per-symbol
    # bars are chronological (oldest first) — hygiene needs the newest last
    aapl = result["AAPL"]
    assert aapl[0].end < aapl[-1].end


async def test_drops_unsound_bars(policy: FreshnessPolicy) -> None:
    provider = FakeBatchProvider(name="batch", bars_count=90, unsound=("MSFT",))
    router = DataRouter([(provider, 10)], policy)

    result = await router.bars_multi(["AAPL", "MSFT"], timeframe="1d", limit=90)

    assert len(result["AAPL"]) == 90  # untouched
    assert len(result["MSFT"]) == 89  # one malformed bar dropped at the boundary


async def test_partial_symbols_absent_on_error(policy: FreshnessPolicy) -> None:
    # A symbol the batch could not fetch (failed chunk / no data) is simply
    # absent from the dict — never fabricated, never aborts the rest.
    provider = FakeBatchProvider(name="batch", bars_count=90, absent=("MSFT",))
    router = DataRouter([(provider, 10)], policy)

    result = await router.bars_multi(["AAPL", "MSFT", "NVDA"], timeframe="1d", limit=90)

    assert set(result) == {"AAPL", "NVDA"}
    assert "MSFT" not in result


async def test_degrades_to_single_symbol_when_unimplemented(policy: FreshnessPolicy) -> None:
    # No provider implements bars_multi → bounded single-symbol degrade so the
    # screener still works against a non-batch stack.
    provider = FakeBatchProvider(name="batch", bars_count=90, unimplemented=True)
    router = DataRouter([(provider, 10)], policy)

    result = await router.bars_multi(["AAPL", "MSFT"], timeframe="1d", limit=90)

    assert set(result) == {"AAPL", "MSFT"}
    assert provider.single_calls == 2  # fell back to per-symbol bars()


async def test_frozen_symbol_dropped(policy: FreshnessPolicy) -> None:
    # A symbol whose newest daily bar is weeks old (frozen feed) is dropped — we
    # cannot rank a stalled name — while healthy symbols survive.
    provider = FakeBatchProvider(name="batch", bars_count=90, frozen=("MSFT",))
    router = DataRouter([(provider, 10)], policy)

    result = await router.bars_multi(["AAPL", "MSFT"], timeframe="1d", limit=90)

    assert set(result) == {"AAPL"}
    assert "MSFT" not in result


async def test_nonretryable_error_fails_over_without_penalty(policy: FreshnessPolicy) -> None:
    # A NON-retryable ProviderError (permanent request/capability mismatch on a
    # healthy provider) must fail over to the next provider WITHOUT demoting the
    # first into the penalty box — mirroring the _route failover contract.
    primary = FakeBatchProvider(name="primary", fail_nonretryable=True)
    backup = FakeBatchProvider(name="backup", bars_count=90)
    router = DataRouter([(primary, 10), (backup, 20)], policy)

    result = await router.bars_multi(["AAPL", "MSFT"], timeframe="1d", limit=90)

    assert set(result) == {"AAPL", "MSFT"}  # served by the backup
    assert backup.multi_calls == 1
    primary_slot = next(s for s in router._slots if s.provider is primary)
    assert primary_slot.consecutive_failures == 0  # NOT penalized
    assert primary_slot.available is True


async def test_retryable_error_penalizes_and_fails_over(policy: FreshnessPolicy) -> None:
    # By contrast a RETRYABLE ProviderError (a real provider fault) DOES penalize
    # the first provider before failing over.
    primary = FakeBatchProvider(name="primary", fail=True)
    backup = FakeBatchProvider(name="backup", bars_count=90)
    router = DataRouter([(primary, 10), (backup, 20)], policy)

    result = await router.bars_multi(["AAPL", "MSFT"], timeframe="1d", limit=90)

    assert set(result) == {"AAPL", "MSFT"}  # served by the backup
    primary_slot = next(s for s in router._slots if s.provider is primary)
    assert primary_slot.consecutive_failures == 1  # penalized
    assert primary_slot.available is False


def _alpaca(handler: Callable[[httpx.Request], httpx.Response]) -> AlpacaDataProvider:
    provider = AlpacaDataProvider(api_key="key_id", options={"secret_key": "shh"})
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _json(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode(),
                         headers={"content-type": "application/json"})


async def test_alpaca_bars_multi_parses_and_paginates() -> None:
    # Two pages keyed by symbol; the provider follows next_page_token, merges
    # per symbol, and returns chronological (oldest-first) Bars.
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.params.get("page_token") == "PAGE2":
            return _json({
                "bars": {
                    "AAPL": [{"t": "2026-07-15T00:00:00Z", "o": 191, "h": 193,
                              "l": 190, "c": 192, "v": 1000}],
                    "MSFT": [{"t": "2026-07-15T00:00:00Z", "o": 401, "h": 405,
                              "l": 400, "c": 404, "v": 2000}],
                },
                "next_page_token": None,
            })
        return _json({
            "bars": {
                "AAPL": [{"t": "2026-07-16T00:00:00Z", "o": 192, "h": 195,
                          "l": 191, "c": 194, "v": 1100}],
                "MSFT": [{"t": "2026-07-16T00:00:00Z", "o": 404, "h": 407,
                          "l": 403, "c": 406, "v": 2100}],
            },
            "next_page_token": "PAGE2",
        })

    provider = _alpaca(handler)
    result = await provider.bars_multi(["AAPL", "MSFT"], timeframe="1d", limit=90)

    # followed pagination: two requests, both to the multi-symbol bars endpoint
    assert len(seen) == 2
    assert seen[0].url.path == "/v2/stocks/bars"
    assert seen[0].url.params.get("symbols") == "AAPL,MSFT"
    assert "/v2/stocks/AAPL/" not in str(seen[0].url)  # not the single-symbol path
    # per-page `limit` is the ENDPOINT MAX, not the small per-symbol bars_limit:
    # multi-symbol pagination counts TOTAL bars across symbols, so sending 90
    # would paginate ~100x and blow the spec §8 4-8 req/screen budget.
    assert seen[0].url.params.get("limit") == "10000"

    assert set(result) == {"AAPL", "MSFT"}
    # both pages merged, chronological by bar-open (the 07-15 page is older, first)
    aapl = result["AAPL"]
    assert [b.start.date().isoformat() for b in aapl] == ["2026-07-15", "2026-07-16"]
    assert str(aapl[-1].close) == "194"
    assert aapl[0].source == "alpaca"


async def test_alpaca_bars_multi_unsupported_timeframe_raises() -> None:
    provider = _alpaca(lambda _req: _json({"bars": {}}))
    with pytest.raises(Exception):  # noqa: B017 - ProviderError, non-retryable
        await provider.bars_multi(["AAPL"], timeframe="7m", limit=90)
