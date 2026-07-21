"""``GET /api/correlation`` pins (rank 6 red-first).

Drives the real ``build_app`` over ``httpx.ASGITransport`` with a fake kernel
(real default ``AppConfig`` — RESEARCH mode needs no broker). The endpoint is
an unconditional operator surface (invariant 6 gates AI-FACING surfaces; the
PM tool carries the ``ai.pm_tools.correlation`` flag): happy-path matrix JSON,
422 on bad input (fewer than 2 symbols, more than the configured max, unknown
method, out-of-range window, stablecoin-quoted pair), crypto-pair
normalization into the CRYPTO-gated batch, and 503 — never a fabricated
matrix — when no pair has coverage or the fetch fails entirely.
"""

from __future__ import annotations

import types
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from poseidon.api.server import build_app
from poseidon.core.config import AppConfig
from poseidon.core.events import EventBus
from poseidon.core.models import Bar
from poseidon.data.base import DataCapability


def _bars_for(symbol: str, start: date, closes: list[float]) -> list[Bar]:
    out: list[Bar] = []
    for i, close in enumerate(closes):
        begin = datetime.combine(start + timedelta(days=i), time(0, 0), tzinfo=UTC)
        c = Decimal(str(close))
        out.append(Bar(symbol=symbol, open=c, high=c, low=c, close=c, volume=1000,
                       start=begin, end=begin + timedelta(days=1), source="fake"))
    return out


class _Router:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars = bars_by_symbol
        self.calls: list[dict[str, Any]] = []

    async def bars_multi(self, symbols: list[str], *, timeframe: str = "1d",
                         limit: int = 90, require: DataCapability | None = None,
                         concurrency: int | None = None) -> dict[str, list[Bar]]:
        self.calls.append({"symbols": list(symbols), "limit": limit,
                           "require": require, "concurrency": concurrency})
        return {s: self._bars[s] for s in symbols if s in self._bars}


def _client(router: _Router, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN_FILE", raising=False)
    kernel = types.SimpleNamespace(
        bus=EventBus(), config=AppConfig(), vault=None, router=router)
    app = build_app(kernel)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1")


def _wiggle(n: int = 40) -> list[float]:
    """n deterministic non-constant closes (default min_overlap is 30)."""
    out: list[float] = []
    value = 100.0
    for i in range(n):
        value *= 1.0 + (0.010, -0.004, 0.006)[i % 3]
        out.append(value)
    return out


_CLOSES = _wiggle()


def _router_with(*symbols: str) -> _Router:
    d0 = date(2026, 1, 5)
    return _Router({
        sym: _bars_for(sym, d0, [c * (k + 1) for c in _CLOSES])
        for k, sym in enumerate(symbols)
    })


async def test_happy_path_matrix_json(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _router_with("AAPL", "MSFT")
    async with _client(router, monkeypatch) as c:
        r = await c.get("/api/correlation",
                        params={"symbols": "aapl,MSFT", "window": 60})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbols"] == ["AAPL", "MSFT"]
    assert body["method"] == "pearson"
    assert body["matrix"][0][0] == 1.0
    assert body["matrix"][0][1] == body["matrix"][1][0]
    assert body["pair_observations"][0][1] == len(_CLOSES) - 1
    # window= reached the gather as the bar limit (window + 1 closes).
    assert all(call["limit"] == 61 for call in router.calls)


async def test_spearman_method_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_router_with("AAPL", "MSFT"), monkeypatch) as c:
        r = await c.get("/api/correlation",
                        params={"symbols": "AAPL,MSFT", "method": "spearman"})
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "spearman"


async def test_crypto_pair_normalized_and_routed_to_crypto_batch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    router = _router_with("BTC/USD", "AAPL")
    async with _client(router, monkeypatch) as c:
        r = await c.get("/api/correlation", params={"symbols": "btc/usd,aapl"})
    assert r.status_code == 200, r.text
    assert r.json()["symbols"] == ["AAPL", "BTC/USD"]
    by_require = {call["require"]: call for call in router.calls}
    assert by_require[DataCapability.CRYPTO]["symbols"] == ["BTC/USD"]
    assert by_require[None]["symbols"] == ["AAPL"]


async def test_unsupported_crypto_quote_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_router_with(), monkeypatch) as c:
        r = await c.get("/api/correlation", params={"symbols": "BTC/USDT,AAPL"})
    assert r.status_code == 422
    assert "BTC/USDT" in r.json()["detail"]


async def test_fewer_than_two_symbols_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_router_with("AAPL"), monkeypatch) as c:
        r = await c.get("/api/correlation", params={"symbols": "AAPL"})
        assert r.status_code == 422
        # Duplicates collapse: AAPL,aapl is ONE symbol, not two.
        r2 = await c.get("/api/correlation", params={"symbols": "AAPL,aapl"})
        assert r2.status_code == 422


async def test_more_than_max_symbols_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    symbols = ",".join(f"S{i:02d}" for i in range(13))  # default max is 12
    router = _router_with()
    async with _client(router, monkeypatch) as c:
        r = await c.get("/api/correlation", params={"symbols": symbols})
    assert r.status_code == 422
    assert "12" in r.json()["detail"]
    assert router.calls == []  # rejected before any fetch


async def test_bad_method_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_router_with("AAPL", "MSFT"), monkeypatch) as c:
        r = await c.get("/api/correlation",
                        params={"symbols": "AAPL,MSFT", "method": "kendall"})
    assert r.status_code == 422


async def test_out_of_range_window_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_router_with("AAPL", "MSFT"), monkeypatch) as c:
        r = await c.get("/api/correlation",
                        params={"symbols": "AAPL,MSFT", "window": 10})
    assert r.status_code == 422


async def test_no_coverage_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_Router({}), monkeypatch) as c:  # nothing served at all
        r = await c.get("/api/correlation", params={"symbols": "AAPL,MSFT"})
    assert r.status_code == 503
    assert "usable" in r.json()["detail"]


async def test_no_overlapping_pair_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both symbols serve bars, but their windows never intersect.
    router = _Router({
        "AAPL": _bars_for("AAPL", date(2026, 1, 5), _CLOSES),
        "MSFT": _bars_for("MSFT", date(2026, 5, 4), _CLOSES),
    })
    async with _client(router, monkeypatch) as c:
        r = await c.get("/api/correlation", params={"symbols": "AAPL,MSFT"})
    assert r.status_code == 503
    assert "overlap" in r.json()["detail"]
