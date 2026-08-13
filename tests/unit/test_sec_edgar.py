# tests/unit/test_sec_edgar.py
"""Keyless SEC EDGAR provider (r2-wave2 rank 4). No network — every test drives
the real ``_get_json``/``_decode`` path over an ``httpx.MockTransport``
(test_coinbase_data.py style).

Pins: registry entry, keyless construction, the identified User-Agent actually
sent on every request (SEC fair-access policy), ticker->CIK zero-padded to 10
digits, companyfacts curation (fallback tag order, USD-family units only,
10-K/10-Q only, newest-first grouping by end date, Decimal(str(val))
exactness, annual-vs-quarterly by form), submissions parallel-array filings
(bounded, comma-split items, accession-nodashes URL), and the polite pacing
throttle.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

import poseidon.data.providers.sec_edgar as sec_edgar_module
from poseidon import __version__
from poseidon.core.errors import ProviderError
from poseidon.data.base import DataCapability
from poseidon.data.providers import BUILTIN_PROVIDERS
from poseidon.data.providers.sec_edgar import SecEdgarProvider

_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}

_FACTS = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {"shares": [
                    {"end": "2025-09-27", "val": 15115823000, "form": "10-K",
                     "filed": "2025-11-01", "fy": 2025, "fp": "FY"},
                ]},
            },
        },
        "us-gaap": {
            # Fallback order: the modern revenue tag must win over legacy Revenues.
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [
                    # annual, original 10-K
                    {"start": "2024-09-29", "end": "2025-09-27", "val": 391035000000,
                     "form": "10-K", "filed": "2025-11-01"},
                    # same period re-reported as a comparative in a LATER filing —
                    # the original (earliest-filed) value must win
                    {"start": "2024-09-29", "end": "2025-09-27", "val": 391035000999,
                     "form": "10-K", "filed": "2026-11-01"},
                    # discrete June quarter
                    {"start": "2025-03-30", "end": "2025-06-28", "val": 85777000000,
                     "form": "10-Q", "filed": "2025-08-01"},
                    # YTD roll-up sharing the SAME end date — must NOT be picked
                    {"start": "2024-09-29", "end": "2025-06-28", "val": 210000000000,
                     "form": "10-Q", "filed": "2025-08-01"},
                    # an 8-K row must be ignored (10-K/10-Q only)
                    {"start": "2025-03-30", "end": "2025-06-28", "val": 1,
                     "form": "8-K", "filed": "2025-08-02"},
                ],
                    # non-USD unit families are ignored
                    "EUR": [{"start": "2024-09-29", "end": "2025-09-27", "val": 5,
                             "form": "10-K", "filed": "2025-11-01"}]},
            },
            "Revenues": {
                "units": {"USD": [
                    {"start": "2024-09-29", "end": "2025-09-27", "val": 7,
                     "form": "10-K", "filed": "2025-11-01"},
                ]},
            },
            "NetIncomeLoss": {
                "units": {"USD": [
                    {"start": "2024-09-29", "end": "2025-09-27", "val": 93736000000,
                     "form": "10-K", "filed": "2025-11-01"},
                    {"start": "2025-03-30", "end": "2025-06-28", "val": 23434000000,
                     "form": "10-Q", "filed": "2025-08-01"},
                ]},
            },
            "Assets": {  # instant concept: end date only
                "units": {"USD": [
                    {"end": "2025-09-27", "val": 364980000000, "form": "10-K",
                     "filed": "2025-11-01"},
                    {"end": "2025-06-28", "val": 331522000000, "form": "10-Q",
                     "filed": "2025-08-01"},
                ]},
            },
            "EarningsPerShareDiluted": {
                "units": {"USD/shares": [
                    {"start": "2024-09-29", "end": "2025-09-27", "val": 6.42,
                     "form": "10-K", "filed": "2025-11-01"},
                ]},
            },
        },
    },
}

_SUBMISSIONS = {
    "cik": "320193",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000042", "0000320193-25-000073",
                                "0000320193-25-000057"],
            "filingDate": ["2026-05-01", "2025-11-01", "2025-08-01"],
            "reportDate": ["2026-04-30", "2025-09-27", "2025-06-28"],
            "form": ["8-K", "10-K", "10-Q"],
            "items": ["2.02,9.01", "", ""],
            "primaryDocument": ["aapl-8k.htm", "aapl-20250927.htm", "aapl-20250628.htm"],
            "primaryDocDescription": ["8-K", "10-K", "10-Q"],
        },
    },
}


def _url_payloads(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    payloads: dict[str, Any] = {
        "https://www.sec.gov/files/company_tickers.json": _TICKERS,
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json": _FACTS,
        "https://data.sec.gov/submissions/CIK0000320193.json": _SUBMISSIONS,
    }
    payloads.update(overrides or {})
    return payloads


def _provider(payloads: dict[str, Any] | None = None,
              options: dict[str, Any] | None = None,
              seen: list[httpx.Request] | None = None) -> SecEdgarProvider:
    table = _url_payloads(payloads)

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        url = str(req.url).split("?")[0]
        if url not in table:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=json.dumps(table[url]).encode(),
                              headers={"content-type": "application/json"})

    provider = SecEdgarProvider(api_key="", options=options)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _fast(monkeypatch: pytest.MonkeyPatch, gap: float = 0.0) -> None:
    monkeypatch.setattr(sec_edgar_module, "_MIN_REQUEST_GAP", gap)


# ----------------------------------------------------------- construction


def test_registered_in_builtin_providers() -> None:
    assert BUILTIN_PROVIDERS.get("sec_edgar") is SecEdgarProvider


def test_keyless_construction_and_capabilities() -> None:
    provider = SecEdgarProvider(api_key="")
    assert provider.name == "sec_edgar"
    caps = provider.capabilities()
    assert caps == frozenset({DataCapability.FUNDAMENTALS, DataCapability.FILINGS})


async def test_declared_user_agent_sent_on_every_request(monkeypatch) -> None:
    _fast(monkeypatch)
    seen: list[httpx.Request] = []
    provider = _provider(options={"user_agent": "shuffman95 test@example.com"}, seen=seen)
    await provider.fundamentals("AAPL")
    assert len(seen) >= 2  # tickers + companyfacts
    assert all(r.headers["User-Agent"] == "shuffman95 test@example.com" for r in seen)


async def test_default_user_agent_identifies_poseidon(monkeypatch) -> None:
    _fast(monkeypatch)
    seen: list[httpx.Request] = []
    provider = _provider(seen=seen)
    await provider.fundamentals("AAPL")
    ua = seen[0].headers["User-Agent"]
    assert f"poseidon/{__version__}" in ua and "user_agent" in ua


# ----------------------------------------------------------- ticker -> CIK


async def test_cik_zero_padded_to_ten_digits_in_urls(monkeypatch) -> None:
    _fast(monkeypatch)
    seen: list[httpx.Request] = []
    provider = _provider(seen=seen)
    await provider.fundamentals("AAPL")
    await provider.filings("AAPL")
    paths = [r.url.path for r in seen]
    assert "/api/xbrl/companyfacts/CIK0000320193.json" in paths
    assert "/submissions/CIK0000320193.json" in paths


async def test_ticker_map_cached_across_calls(monkeypatch) -> None:
    _fast(monkeypatch)
    seen: list[httpx.Request] = []
    provider = _provider(seen=seen)
    await provider.fundamentals("AAPL")
    await provider.fundamentals("aapl")  # case-insensitive; cache hit
    ticker_fetches = [r for r in seen if r.url.path == "/files/company_tickers.json"]
    assert len(ticker_fetches) == 1


async def test_concurrent_cold_start_fetches_ticker_map_once(monkeypatch) -> None:
    # Cold-start race: the cache check and the fill are separated by an await,
    # so N coroutines arriving together each see an empty map and each fire the
    # (large, rate-limited) tickers download. One lock, one fetch.
    #
    # The handler must be ASYNC and actually yield: a plain sync MockTransport
    # handler completes without ever returning to the event loop, so the first
    # caller fills the cache before the others run and the race silently fails
    # to reproduce. Real network I/O always yields here.
    _fast(monkeypatch)
    seen: list[httpx.Request] = []
    table = _url_payloads()

    async def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        await asyncio.sleep(0)  # the yield a real socket would give us
        url = str(req.url).split("?")[0]
        return httpx.Response(200, content=json.dumps(table[url]).encode(),
                              headers={"content-type": "application/json"})

    provider = SecEdgarProvider(api_key="")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await asyncio.gather(*(provider.fundamentals("AAPL") for _ in range(5)))
    ticker_fetches = [r for r in seen if r.url.path == "/files/company_tickers.json"]
    assert len(ticker_fetches) == 1


async def test_unknown_ticker_raises_non_retryable(monkeypatch) -> None:
    _fast(monkeypatch)
    provider = _provider()
    with pytest.raises(ProviderError) as exc_info:
        await provider.fundamentals("ZZZQ")
    assert exc_info.value.retryable is False


# ----------------------------------------------------------- fundamentals


async def test_fundamentals_curation(monkeypatch) -> None:
    _fast(monkeypatch)
    report = await _provider().fundamentals("AAPL")

    assert report.symbol == "AAPL" and report.source == "sec_edgar"
    assert report.overview is not None and report.overview.name == "Apple Inc."
    # never fabricate ratios: the overview carries the entity name ONLY
    assert report.overview.pe_ratio is None and report.overview.market_cap is None

    # newest-first grouping by end date
    ends = [p.fiscal_date_ending for p in report.statements]
    assert ends == [date(2025, 9, 27), date(2025, 6, 28)]

    annual, quarterly = report.statements
    # annual-vs-quarterly classification by form
    assert annual.period == "annual" and annual.form == "10-K"
    assert quarterly.period == "quarterly" and quarterly.form == "10-Q"
    assert annual.filed == date(2025, 11, 1) and quarterly.filed == date(2025, 8, 1)
    assert annual.currency == "USD"

    # fallback tag order: modern revenue tag (not legacy Revenues = 7), and the
    # ORIGINAL filing's value (not the later comparative re-report ...999)
    assert annual.items["revenue"] == Decimal("391035000000")
    assert annual.items["net_income"] == Decimal("93736000000")
    assert annual.items["total_assets"] == Decimal("364980000000")
    assert annual.items["diluted_eps"] == Decimal("6.42")
    assert annual.items["shares_outstanding"] == Decimal("15115823000")

    # quarterly: discrete 3-month figure, never the YTD roll-up (210e9) and
    # never the 8-K row (1)
    assert quarterly.items["revenue"] == Decimal("85777000000")
    assert quarterly.items["net_income"] == Decimal("23434000000")
    assert quarterly.items["total_assets"] == Decimal("331522000000")


async def test_fundamentals_decimal_exactness(monkeypatch) -> None:
    _fast(monkeypatch)
    report = await _provider().fundamentals("AAPL")
    annual = report.statements[0]
    assert isinstance(annual.items["revenue"], Decimal)
    assert str(annual.items["revenue"]) == "391035000000"
    payload = report.model_dump(mode="json")
    assert payload["statements"][0]["items"]["revenue"] == "391035000000"


async def test_fundamentals_empty_facts_raises_non_retryable(monkeypatch) -> None:
    _fast(monkeypatch)
    provider = _provider({
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json":
            {"cik": 320193, "entityName": "Apple Inc.", "facts": {}},
    })
    with pytest.raises(ProviderError) as exc_info:
        await provider.fundamentals("AAPL")
    assert exc_info.value.retryable is False


async def test_fundamentals_uncurated_tags_raise_non_retryable(monkeypatch) -> None:
    _fast(monkeypatch)
    provider = _provider({
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json": {
            "cik": 320193, "entityName": "Apple Inc.",
            "facts": {"us-gaap": {"SomeObscureTag": {"units": {"USD": [
                {"end": "2025-09-27", "val": 1, "form": "10-K", "filed": "2025-11-01"},
            ]}}}},
        },
    })
    with pytest.raises(ProviderError) as exc_info:
        await provider.fundamentals("AAPL")
    assert exc_info.value.retryable is False


# ----------------------------------------------------------- filings


async def test_filings_from_parallel_arrays(monkeypatch) -> None:
    _fast(monkeypatch)
    filings = await _provider().filings("AAPL", limit=10)
    assert [f.form for f in filings] == ["8-K", "10-K", "10-Q"]  # newest first
    eight_k = filings[0]
    assert eight_k.symbol == "AAPL" and eight_k.source == "sec_edgar"
    assert eight_k.filed == date(2026, 5, 1)
    assert eight_k.period_end == date(2026, 4, 30)
    assert eight_k.accession == "0000320193-26-000042"
    assert eight_k.items == ["2.02", "9.01"]  # comma-split
    # accession-nodashes document URL, UNPADDED cik in the archives path
    assert eight_k.url == ("https://www.sec.gov/Archives/edgar/data/320193/"
                           "000032019326000042/aapl-8k.htm")
    assert filings[1].items == []


async def test_filings_bounded_by_limit(monkeypatch) -> None:
    _fast(monkeypatch)
    filings = await _provider().filings("AAPL", limit=2)
    assert len(filings) == 2


# ----------------------------------------------------------- pacing


async def test_pacing_enforces_min_gap_between_requests(monkeypatch) -> None:
    _fast(monkeypatch, gap=0.05)
    stamps: list[float] = []
    table = _url_payloads()

    def handler(req: httpx.Request) -> httpx.Response:
        stamps.append(time.monotonic())
        url = str(req.url).split("?")[0]
        return httpx.Response(200, content=json.dumps(table[url]).encode(),
                              headers={"content-type": "application/json"})

    provider = SecEdgarProvider(api_key="")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await provider.filings("AAPL")  # tickers + submissions = 2 paced requests
    assert len(stamps) == 2
    assert stamps[1] - stamps[0] >= 0.04  # monotonic gap held (small tolerance)
