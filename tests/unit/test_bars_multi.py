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
from poseidon.data.base import DataCapability
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


async def test_require_crypto_routes_only_to_crypto_provider(policy: FreshnessPolicy) -> None:
    # An equity-only batch provider (BARS, no CRYPTO) and a crypto batch provider
    # (BARS+CRYPTO) are both configured. A require=CRYPTO batch must select ONLY
    # the crypto provider — an equity provider silently drops /USD symbols and
    # would return {} SUCCESSFULLY, starving the screen.
    equity = FakeBatchProvider(name="equity", bars_count=90)
    crypto = FakeBatchProvider(name="coinbase", bars_count=90, crypto=True)
    router = DataRouter([(equity, 10), (crypto, 20)], policy)

    result = await router.bars_multi(
        ["BTC/USD", "ETH/USD"], timeframe="1d", limit=90,
        require=DataCapability.CRYPTO,
    )

    assert set(result) == {"BTC/USD", "ETH/USD"}
    assert crypto.multi_calls == 1
    assert equity.multi_calls == 0  # equity provider never consulted for crypto


async def test_require_crypto_no_capable_provider_returns_empty(policy: FreshnessPolicy) -> None:
    # No CRYPTO-capable provider configured → capable set empty → {} (never an
    # equity provider serving a /USD pair, never a crash).
    equity = FakeBatchProvider(name="equity", bars_count=90)
    router = DataRouter([(equity, 10)], policy)

    result = await router.bars_multi(
        ["BTC/USD", "ETH/USD"], timeframe="1d", limit=90,
        require=DataCapability.CRYPTO,
    )

    assert result == {}
    assert equity.multi_calls == 0


async def test_require_none_equity_path_unchanged(policy: FreshnessPolicy) -> None:
    # require=None (the equity/default path) keeps the capable set byte-identical
    # to today: an equity-only provider still serves the batch.
    equity = FakeBatchProvider(name="equity", bars_count=90)
    router = DataRouter([(equity, 10)], policy)

    result = await router.bars_multi(["AAPL", "MSFT"], timeframe="1d", limit=90)

    assert set(result) == {"AAPL", "MSFT"}
    assert equity.multi_calls == 1


async def test_concurrency_bounds_single_symbol_degrade(policy: FreshnessPolicy) -> None:
    # Coinbase has no batch endpoint → NotImplementedError → single-symbol degrade.
    # concurrency=2 must cap the in-flight fan-out; a small per-call delay forces
    # overlap so peak concurrency is observable.
    provider = FakeBatchProvider(
        name="coinbase", bars_count=90, crypto=True,
        unimplemented=True, single_delay=0.02,
    )
    router = DataRouter([(provider, 10)], policy)

    symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "ADA/USD"]
    result = await router.bars_multi(
        symbols, timeframe="1d", limit=90,
        require=DataCapability.CRYPTO, concurrency=2,
    )

    assert set(result) == set(symbols)
    assert provider.single_calls == len(symbols)
    assert provider.max_single_active <= 2  # semaphore honored the concurrency cap


async def test_concurrency_default_when_unset(policy: FreshnessPolicy) -> None:
    # concurrency=None keeps the existing default (16): a small fan-out runs fully
    # parallel, unchanged from today.
    provider = FakeBatchProvider(
        name="batch", bars_count=90, unimplemented=True, single_delay=0.02,
    )
    router = DataRouter([(provider, 10)], policy)

    symbols = ["AAPL", "MSFT", "NVDA", "AMZN"]
    result = await router.bars_multi(symbols, timeframe="1d", limit=90)

    assert set(result) == set(symbols)
    assert provider.max_single_active == 4  # all four ran concurrently (< default 16)


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
