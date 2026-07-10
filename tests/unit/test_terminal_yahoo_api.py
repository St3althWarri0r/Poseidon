"""Public data functions: request shapes, caching, batch fallback (no network)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import poseidon.terminal.yahoo as ty
from poseidon.core.errors import DataError


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ty, "_cache", ty.TTLCache())
    monkeypatch.setattr(ty, "_session", None)


def install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    s = ty.YahooSession(client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    monkeypatch.setattr(ty, "_session", s)
    return seen


def bootstrap_ok(req: httpx.Request) -> httpx.Response | None:
    if req.url.host == "fc.yahoo.com":
        return httpx.Response(404)
    if req.url.path == "/v1/test/getcrumb":
        return httpx.Response(200, text="C")
    return None


async def test_get_quotes_batches_sorts_dedupes_and_caches(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        assert req.url.params["symbols"] == "AAPL,MSFT"  # deduped + sorted
        return httpx.Response(200, json={"quoteResponse": {"result": [
            {"symbol": "AAPL", "regularMarketPrice": 1.0},
            {"symbol": "MSFT", "regularMarketPrice": 2.0},
            {"symbol": "DEAD", "quoteType": "NONE"},
        ]}})

    seen = install(monkeypatch, handler)
    out = await ty.get_quotes(["msft", "AAPL", "aapl"])
    assert [q["symbol"] for q in out] == ["AAPL", "MSFT"]  # NONE filtered
    n = len(seen)
    await ty.get_quotes(["AAPL", "MSFT"])  # same key -> cache, no new request
    assert len(seen) == n


async def test_get_quotes_falls_back_per_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        syms = req.url.params["symbols"]
        if "," in syms:
            return httpx.Response(500)
        if syms == "BAD":
            return httpx.Response(500)
        return httpx.Response(200, json={"quoteResponse": {"result": [
            {"symbol": syms, "regularMarketPrice": 9.9}]}})

    install(monkeypatch, handler)
    out = await ty.get_quotes(["GOOD", "BAD"])
    assert [q["symbol"] for q in out] == ["GOOD"]  # one failure can't blank the panel


async def test_get_chart_params_and_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v8/finance/chart/AAPL"
        assert req.url.params["interval"] == "1d"
        assert int(req.url.params["period1"]) < int(req.url.params["period2"])
        assert "crumb" not in req.url.params
        return httpx.Response(200, json={"chart": {"result": [{
            "meta": {"symbol": "AAPL", "currency": "USD",
                     "fullExchangeName": "NasdaqGS",
                     "regularMarketPrice": 314.66, "previousClose": 313.39},
            "timestamp": [100], "indicators": {"quote": [{
                "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
                "volume": [10]}]},
        }]}})

    install(monkeypatch, handler)
    out = await ty.get_chart("aapl", "1M")
    assert out["symbol"] == "AAPL" and out["exchangeName"] == "NasdaqGS"
    assert out["candles"] == [{"time": 100, "open": 1.0, "high": 2.0, "low": 0.5,
                               "close": 1.5, "volume": 10}]


async def test_get_chart_rejects_bad_range(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda req: httpx.Response(500))
    with pytest.raises(DataError, match="range"):
        await ty.get_chart("AAPL", "2W")


async def test_search_and_news_share_search_endpoint(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/finance/search"
        if req.url.params["newsCount"] == "0":
            return httpx.Response(200, json={"quotes": [{"symbol": "AAPL"}], "news": []})
        assert req.url.params["newsCount"] == "12"
        return httpx.Response(200, json={"quotes": [], "news": [
            {"title": "T", "link": "https://x", "providerPublishTime": 1}]})

    install(monkeypatch, handler)
    assert (await ty.search_symbols("apple"))[0]["symbol"] == "AAPL"
    assert (await ty.get_news("AAPL"))[0]["publishedAt"] == 1000
    assert (await ty.get_news(None))[0]["title"] == "T"  # market-wide query


async def test_fundamentals_simplifies_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        assert req.url.path == "/v10/finance/quoteSummary/AAPL"
        assert req.url.params["modules"] == (
            "assetProfile,summaryDetail,financialData,defaultKeyStatistics,price")
        return httpx.Response(200, json={"quoteSummary": {"result": [{
            "summaryDetail": {"marketCap": {"raw": 3.1e12, "fmt": "3.1T"}},
            "financialData": {"debtToEquity": {"raw": 79.55, "fmt": "79.55%"}},
            "price": {"longName": "Apple Inc."},
        }]}})

    install(monkeypatch, handler)
    f = await ty.get_fundamentals("AAPL")
    assert f["valuation"]["marketCap"] == 3.1e12
    assert f["financials"]["debtToEquity"] == 0.7955


async def test_market_overview_shape_and_sector_sort(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        syms = req.url.params["symbols"].split(",")
        return httpx.Response(200, json={"quoteResponse": {"result": [
            {"symbol": s, "regularMarketChangePercent":
                (None if s == "XLC" else float(i))}
            for i, s in enumerate(syms)]}})

    install(monkeypatch, handler)
    mkt = await ty.get_market_overview()
    assert set(mkt) == {"indices", "futures", "rates", "commodities", "crypto",
                        "currencies", "sectors"}
    assert [q["symbol"] for q in mkt["indices"]] == ["^GSPC", "^DJI", "^IXIC", "^RUT", "^VIX"]
    sectors = mkt["sectors"]
    assert len(sectors) == 11 and sectors[-1]["symbol"] == "XLC"  # null sorts last
    changes = [s["changePercent"] for s in sectors if s["changePercent"] is not None]
    assert changes == sorted(changes, reverse=True)
    assert sectors[0]["name"]  # names come from SECTOR_ETFS
