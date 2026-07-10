"""Route contract: params, envelopes, cache headers (ASGI, no network)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import poseidon.terminal.yahoo as ty
from poseidon.core.errors import DataError
from poseidon.terminal.routes import router


def client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def test_quote_param_validation_and_header(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(symbols: list[str]) -> list[dict[str, Any]]:
        return [{"symbol": s} for s in symbols]

    monkeypatch.setattr(ty, "get_quotes", fake)
    async with client() as c:
        r = await c.get("/api/terminal/quote?symbols=AAPL,MSFT")
        assert r.status_code == 200 and [q["symbol"] for q in r.json()] == ["AAPL", "MSFT"]
        assert r.headers["cache-control"] == "public, s-maxage=10, stale-while-revalidate=40"
        assert (await c.get("/api/terminal/quote")).status_code == 400
        many = ",".join(f"S{i}" for i in range(61))
        r = await c.get(f"/api/terminal/quote?symbols={many}")
        assert r.status_code == 400 and "max 60" in r.json()["error"]


async def test_chart_validation_and_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(symbol: str, range_key: str) -> dict[str, Any]:
        raise DataError("Yahoo returned HTTP 500")

    monkeypatch.setattr(ty, "get_chart", boom)
    async with client() as c:
        assert (await c.get("/api/terminal/chart?range=1M")).status_code == 400
        assert (await c.get("/api/terminal/chart?symbol=AAPL&range=2W")).status_code == 400
        r = await c.get("/api/terminal/chart?symbol=AAPL&range=1M")
        assert r.status_code == 502 and r.json() == {"error": "Yahoo returned HTTP 500"}


async def test_chart_cache_header_by_range(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(symbol: str, range_key: str) -> dict[str, Any]:
        return {"symbol": symbol, "candles": []}

    monkeypatch.setattr(ty, "get_chart", fake)
    async with client() as c:
        intraday = await c.get("/api/terminal/chart?symbol=AAPL&range=1D")
        daily = await c.get("/api/terminal/chart?symbol=AAPL&range=1Y")
        assert "s-maxage=30" in intraday.headers["cache-control"]
        assert "s-maxage=120" in daily.headers["cache-control"]


async def test_search_empty_is_ok_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    async with client() as c:
        r = await c.get("/api/terminal/search?q=")
        assert r.status_code == 200 and r.json() == []


async def test_news_and_market_and_fundamentals(monkeypatch: pytest.MonkeyPatch) -> None:
    async def news(symbol: str | None) -> list[dict[str, Any]]:
        return [{"title": f"news-for-{symbol}"}]

    async def market() -> dict[str, Any]:
        return {"indices": []}

    async def funda(symbol: str) -> dict[str, Any]:
        return {"symbol": symbol}

    monkeypatch.setattr(ty, "get_news", news)
    monkeypatch.setattr(ty, "get_market_overview", market)
    monkeypatch.setattr(ty, "get_fundamentals", funda)
    async with client() as c:
        assert (await c.get("/api/terminal/news")).json()[0]["title"] == "news-for-None"
        r = await c.get("/api/terminal/news?symbol=AAPL")
        assert r.json()[0]["title"] == "news-for-AAPL"
        assert "s-maxage=30" in r.headers["cache-control"]
        assert (await c.get("/api/terminal/market")).json() == {"indices": []}
        assert (await c.get("/api/terminal/fundamentals?symbol=AAPL")).json() == {
            "symbol": "AAPL"}
        assert (await c.get("/api/terminal/fundamentals")).status_code == 400
