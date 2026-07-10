"""YahooSession crumb flow against httpx.MockTransport (no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from poseidon.core.errors import DataError
from poseidon.terminal.yahoo import YahooSession


def make_session(handler: httpx.MockTransport) -> YahooSession:
    return YahooSession(client=httpx.AsyncClient(transport=handler))


async def test_crumb_fetched_once_and_attached() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404, headers={"set-cookie": "A3=abc; Domain=.yahoo.com"})
        if req.url.path == "/v1/test/getcrumb":
            return httpx.Response(200, text="CRUMB123")
        if req.url.path == "/v7/finance/quote":
            assert req.url.params["crumb"] == "CRUMB123"
            return httpx.Response(200, json={"quoteResponse": {"result": []}})
        raise AssertionError(f"unexpected {req.url}")

    s = make_session(httpx.MockTransport(handler))
    out = await s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                           {"symbols": "AAPL"}, needs_crumb=True)
    assert out == {"quoteResponse": {"result": []}}
    # Second call reuses the cached crumb (no extra bootstrap requests).
    n = len(seen)
    await s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                     {"symbols": "MSFT"}, needs_crumb=True)
    assert len(seen) == n + 1


async def test_no_crumb_endpoints_skip_bootstrap() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "crumb" not in req.url.params
        assert req.url.path.startswith("/v8/finance/chart/")
        return httpx.Response(200, json={"chart": {"result": [{}]}})

    s = make_session(httpx.MockTransport(handler))
    out = await s.get_json("https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
                           {"interval": "1d"})
    assert out == {"chart": {"result": [{}]}}


async def test_forbidden_triggers_one_rebootstrap_then_succeeds() -> None:
    crumbs = iter(["OLD", "NEW"])
    quote_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if req.url.path == "/v1/test/getcrumb":
            return httpx.Response(200, text=next(crumbs))
        if req.url.path == "/v7/finance/quote":
            quote_calls.append(req.url.params["crumb"])
            if req.url.params["crumb"] == "OLD":
                return httpx.Response(401)
            return httpx.Response(200, json={"quoteResponse": {"result": []}})
        raise AssertionError(str(req.url))

    s = make_session(httpx.MockTransport(handler))
    await s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                     {"symbols": "AAPL"}, needs_crumb=True)
    assert quote_calls == ["OLD", "NEW"]


async def test_upstream_error_raises_dataerror() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    s = make_session(httpx.MockTransport(handler))
    with pytest.raises(DataError):
        await s.get_json("https://query2.finance.yahoo.com/v8/finance/chart/AAPL", {})


async def test_malformed_json_on_200_raises_dataerror() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json")

    s = make_session(httpx.MockTransport(handler))
    with pytest.raises(DataError):
        await s.get_json("https://query2.finance.yahoo.com/v8/finance/chart/AAPL", {})


async def test_cold_start_bootstrap_is_single_flight() -> None:
    crumb_fetches = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal crumb_fetches
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if req.url.path == "/v1/test/getcrumb":
            crumb_fetches += 1
            return httpx.Response(200, text="C")
        return httpx.Response(200, json={"quoteResponse": {"result": []}})

    s = make_session(httpx.MockTransport(handler))
    await asyncio.gather(
        s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                   {"symbols": "AAPL"}, needs_crumb=True),
        s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                   {"symbols": "MSFT"}, needs_crumb=True),
    )
    assert crumb_fetches == 1  # the second coroutine reuses the first handshake


async def test_session_state_persists_and_reloads(tmp_path: Path) -> None:
    state = tmp_path / "terminal-yahoo.json"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(
                404, headers={"set-cookie": "A3=abc; Domain=.yahoo.com; Path=/"})
        if req.url.path == "/v1/test/getcrumb":
            return httpx.Response(200, text="CRUMB1")
        assert req.url.params["crumb"] == "CRUMB1"
        return httpx.Response(200, json={"quoteResponse": {"result": []}})

    s1 = YahooSession(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                      state_path=state)
    await s1.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                      {"symbols": "AAPL"}, needs_crumb=True)
    assert state.exists()
    assert oct(state.stat().st_mode)[-3:] == "600"

    def handler2(req: httpx.Request) -> httpx.Response:
        assert req.url.host != "fc.yahoo.com", "restored session must not re-bootstrap"
        assert req.url.path != "/v1/test/getcrumb"
        assert req.url.params["crumb"] == "CRUMB1"
        assert "A3=abc" in req.headers.get("cookie", "")
        return httpx.Response(200, json={"quoteResponse": {"result": []}})

    s2 = YahooSession(client=httpx.AsyncClient(transport=httpx.MockTransport(handler2)),
                      state_path=state)
    await s2.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                      {"symbols": "AAPL"}, needs_crumb=True)


async def test_corrupt_state_file_starts_clean(tmp_path: Path) -> None:
    state = tmp_path / "terminal-yahoo.json"
    state.write_text("{definitely not json", encoding="utf-8")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if req.url.path == "/v1/test/getcrumb":
            return httpx.Response(200, text="FRESH")
        assert req.url.params["crumb"] == "FRESH"
        return httpx.Response(200, json={"quoteResponse": {"result": []}})

    s = YahooSession(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     state_path=state)
    out = await s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                           {"symbols": "AAPL"}, needs_crumb=True)
    assert out == {"quoteResponse": {"result": []}}
