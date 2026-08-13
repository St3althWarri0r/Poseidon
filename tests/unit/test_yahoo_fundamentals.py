# tests/unit/test_yahoo_fundamentals.py
"""Keyless Yahoo quoteSummary overview provider (r2-wave2 rank 4). No network.

The provider ports the terminal's endpoint knowledge (cookie+crumb handshake,
quoteSummary modules, formatted=false) provider-locally: it must NOT import
poseidon.terminal.yahoo (whose docstring contract is 'never used by the trading
data router'). Pins: handshake-then-fetch order, the retry-once-on-401 crumb
refresh, exact Decimal money fields, float ratios, and the non-retryable empty
result."""
from __future__ import annotations

import ast
import inspect
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

import poseidon.data.providers.yahoo_fundamentals as yahoo_fundamentals_module
from poseidon.core.errors import ProviderError
from poseidon.data.base import DataCapability
from poseidon.data.providers import BUILTIN_PROVIDERS
from poseidon.data.providers.yahoo_fundamentals import YahooFundamentalsProvider

_RESULT = {
    "quoteSummary": {
        "result": [
            {
                "assetProfile": {
                    "sector": "Technology",
                    "industry": "Consumer Electronics",
                    "longBusinessSummary": "Apple Inc. designs smartphones.",
                },
                "summaryDetail": {
                    "marketCap": 3400120000000,
                    "trailingPE": 34.2,
                    "forwardPE": 29.1,
                    "dividendYield": 0.0044,
                    "beta": 1.24,
                },
                "financialData": {
                    "totalRevenue": 391035000000,
                    "targetMeanPrice": 252.5,
                    "profitMargins": 0.152,
                    "operatingMargins": 0.31,
                    "returnOnEquity": 1.47,
                },
                "defaultKeyStatistics": {
                    "trailingEps": 6.42,
                    "sharesOutstanding": 15115823000,
                    "pegRatio": 2.5,
                    "priceToBook": 48.1,
                    "enterpriseToEbitda": {"raw": 25.4},  # residual raw-dict unwrap
                },
                "price": {"longName": "Apple Inc.", "shortName": "Apple"},
            }
        ],
        "error": None,
    },
}


class _Yahoo:
    """Scriptable Yahoo endpoint set: cookie URLs, crumb, quoteSummary."""

    def __init__(self, result: Any = None, *, reject_first_crumb: int = 0) -> None:
        self.result = _RESULT if result is None else result
        self.crumb_calls = 0
        self.summary_calls = 0
        self.rejects_left = reject_first_crumb
        self.requests: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        host, path = req.url.host, req.url.path
        if host in ("fc.yahoo.com", "finance.yahoo.com"):
            return httpx.Response(404, content=b"", headers={
                "set-cookie": "A3=poseidon-test; Domain=.yahoo.com; Path=/"})
        if path.endswith("/v1/test/getcrumb"):
            self.crumb_calls += 1
            return httpx.Response(200, content=f"crumb{self.crumb_calls}".encode())
        if "/v10/finance/quoteSummary/" in path:
            self.summary_calls += 1
            if self.rejects_left > 0:
                self.rejects_left -= 1
                return httpx.Response(401, content=b"Unauthorized")
            return httpx.Response(200, content=json.dumps(self.result).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(500, content=b"unexpected url")


def _provider(yahoo: _Yahoo) -> YahooFundamentalsProvider:
    provider = YahooFundamentalsProvider(api_key="")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(yahoo.handler))
    return provider


def test_registered_and_keyless_overview_only() -> None:
    assert BUILTIN_PROVIDERS.get("yahoo_fundamentals") is YahooFundamentalsProvider
    provider = YahooFundamentalsProvider(api_key="")
    assert provider.name == "yahoo_fundamentals"
    assert provider.capabilities() == frozenset({DataCapability.FUNDAMENTALS})


def test_never_imports_terminal_yahoo() -> None:
    # The terminal module's contract ('Study data only — never used by the
    # trading data router') stays true: the provider ports the endpoint
    # knowledge, it does not import the terminal display path.
    tree = ast.parse(inspect.getsource(yahoo_fundamentals_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any("terminal" in a.name for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert "terminal" not in (node.module or "")


async def test_handshake_then_summary_with_crumb() -> None:
    yahoo = _Yahoo()
    report = await _provider(yahoo).fundamentals("AAPL")

    # cookie fetch happened before the crumb, crumb before the summary
    hosts = [r.url.host for r in yahoo.requests]
    assert hosts[0] in ("fc.yahoo.com", "finance.yahoo.com")
    assert yahoo.crumb_calls == 1 and yahoo.summary_calls == 1
    summary_req = yahoo.requests[-1]
    assert "/v10/finance/quoteSummary/AAPL" in summary_req.url.path
    assert summary_req.url.params.get("crumb") == "crumb1"
    assert summary_req.url.params.get("formatted") == "false"
    assert summary_req.url.params.get("modules") == (
        "assetProfile,summaryDetail,financialData,defaultKeyStatistics,price")

    assert report.symbol == "AAPL" and report.source == "yahoo_fundamentals"
    assert report.statements == []  # overview-only secondary source


async def test_overview_decimals_exact_and_ratios_float() -> None:
    report = await _provider(_Yahoo()).fundamentals("AAPL")
    ov = report.overview
    assert ov is not None
    assert ov.name == "Apple Inc."
    assert ov.sector == "Technology" and ov.industry == "Consumer Electronics"
    assert ov.description == "Apple Inc. designs smartphones."
    assert ov.market_cap == Decimal("3400120000000") and isinstance(ov.market_cap, Decimal)
    assert ov.revenue_ttm == Decimal("391035000000")
    assert ov.eps_ttm == Decimal("6.42")
    assert ov.analyst_target == Decimal("252.5")
    assert ov.shares_outstanding == Decimal("15115823000")
    assert ov.pe_ratio == pytest.approx(34.2)
    assert ov.forward_pe == pytest.approx(29.1)
    assert ov.ev_to_ebitda == pytest.approx(25.4)  # {'raw': ...} unwrapped
    assert ov.beta == pytest.approx(1.24)


async def test_401_once_triggers_one_rehandshake_retry() -> None:
    yahoo = _Yahoo(reject_first_crumb=1)
    report = await _provider(yahoo).fundamentals("AAPL")
    assert yahoo.summary_calls == 2      # rejected once, retried once
    assert yahoo.crumb_calls == 2        # re-handshake fetched a fresh crumb
    summary_reqs = [r for r in yahoo.requests if "/v10/finance/" in r.url.path]
    assert summary_reqs[-1].url.params.get("crumb") == "crumb2"
    assert report.overview is not None


async def test_persistent_401_raises_provider_error() -> None:
    yahoo = _Yahoo(reject_first_crumb=99)
    with pytest.raises(ProviderError):
        await _provider(yahoo).fundamentals("AAPL")
    assert yahoo.summary_calls == 2  # exactly one retry, never a loop


async def test_empty_result_raises_non_retryable() -> None:
    for empty in ({"quoteSummary": {"result": [], "error": None}},
                  {"quoteSummary": {"result": [{}], "error": None}},
                  {}):
        yahoo = _Yahoo(result=empty)
        with pytest.raises(ProviderError) as exc_info:
            await _provider(yahoo).fundamentals("AAPL")
        assert exc_info.value.retryable is False
