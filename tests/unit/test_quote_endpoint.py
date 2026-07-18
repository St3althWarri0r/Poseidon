"""`/api/quote/{symbol:path}` crypto-pair support (Task 7).

The ticket "Quote" button fetches ``/api/quote/<symbol>``. A crypto pair like
``BTC/USD`` contains a slash, so the original single-segment ``{symbol}``
converter 404s before routing ever reaches the provider. This drives the real
``build_app`` app over ASGI with a fake router/clock and asserts a crypto quote
comes back for the slash-form symbol (both raw and percent-encoded, the shape
the JS sends via ``encodeURIComponent``), while a plain equity ticker still
works unchanged.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from poseidon.api.server import build_app
from poseidon.core.enums import MarketSession
from poseidon.core.events import EventBus
from poseidon.core.models import Quote


class _FakeRouter:
    """Records the symbol the endpoint asked for and returns a fresh Quote."""

    def __init__(self) -> None:
        self.asked: str | None = None

    async def quote(self, symbol: str, *, allow_delayed: bool = False) -> Quote:
        self.asked = symbol
        return Quote(
            symbol=symbol,
            bid=Decimal("64000.12"),
            ask=Decimal("64010.34"),
            last=Decimal("64005.00"),
            as_of=datetime(2026, 7, 17, tzinfo=UTC),
            source="fake",
        )

    async def reference_quote(self, symbol: str) -> Quote:  # pragma: no cover
        self.asked = symbol
        return await self.quote(symbol)


def _client() -> tuple[httpx.AsyncClient, _FakeRouter]:
    router = _FakeRouter()
    dashboard = types.SimpleNamespace(host="127.0.0.1", port=8799,
                                      auth_token_credential=None)
    kernel = types.SimpleNamespace(
        bus=EventBus(),
        config=types.SimpleNamespace(dashboard=dashboard),
        vault=None,
        router=router,
        clock=types.SimpleNamespace(session=lambda: MarketSession.REGULAR),
    )
    app = build_app(kernel)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://127.0.0.1")
    return client, router


async def test_quote_serves_crypto_pair_raw_slash() -> None:
    client, router = _client()
    async with client as c:
        r = await c.get("/api/quote/BTC/USD")
    assert r.status_code == 200, r.text
    assert router.asked == "BTC/USD"
    body = r.json()
    assert body["symbol"] == "BTC/USD"
    assert body["reference"] is False
    assert body["ask"] == "64010.34"


async def test_quote_serves_crypto_pair_percent_encoded() -> None:
    # The dashboard JS builds the URL with encodeURIComponent("BTC/USD") ->
    # "BTC%2FUSD"; the :path converter must still resolve it to the pair.
    client, router = _client()
    async with client as c:
        r = await c.get("/api/quote/BTC%2FUSD")
    assert r.status_code == 200, r.text
    assert router.asked == "BTC/USD"
    assert r.json()["symbol"] == "BTC/USD"


async def test_quote_equity_ticker_unchanged() -> None:
    client, router = _client()
    async with client as c:
        r = await c.get("/api/quote/aapl")
    assert r.status_code == 200, r.text
    assert router.asked == "AAPL"
    assert r.json()["symbol"] == "AAPL"


def test_ticket_shows_crypto_symbol_hint() -> None:
    import pathlib

    html = pathlib.Path("src/poseidon/api/static/index.html").read_text(encoding="utf-8")
    assert 'id="tk-symbol-hint"' in html
    assert "BTC/USD" in html
