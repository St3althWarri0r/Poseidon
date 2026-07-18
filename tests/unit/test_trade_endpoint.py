"""`/api/trade` asset-class auto-detect (Task 6).

Drives the real ``build_app`` FastAPI app over an ASGI transport with a fake
kernel whose ``order_manager`` merely captures the ``Order`` the endpoint built,
so we can assert exactly how the symbol was classified before submission. No
network, no DB, no risk engine — the endpoint's classification is the unit under
test.
"""

from __future__ import annotations

import types

import httpx

from poseidon.api.server import build_app
from poseidon.core.enums import AssetClass, OrderStatus
from poseidon.core.events import EventBus
from poseidon.core.models import Order


class _CaptureManager:
    """Stands in for kernel.order_manager: records the built Order and echoes
    it back as a (fake) fill so the endpoint's success envelope is produced."""

    def __init__(self) -> None:
        self.order: Order | None = None

    async def submit_manual(self, order: Order) -> Order:
        self.order = order
        order.status = OrderStatus.FILLED
        order.status_reason = ""
        return order


def _client() -> tuple[httpx.AsyncClient, _CaptureManager]:
    mgr = _CaptureManager()
    dashboard = types.SimpleNamespace(host="127.0.0.1", port=8799,
                                      auth_token_credential=None)
    kernel = types.SimpleNamespace(
        bus=EventBus(),
        config=types.SimpleNamespace(dashboard=dashboard),
        vault=None,
        order_manager=mgr,
    )
    app = build_app(kernel)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://127.0.0.1")
    return client, mgr


async def test_trade_autodetects_crypto_from_slash_symbol() -> None:
    client, mgr = _client()
    async with client as c:
        r = await c.post("/api/trade", json={
            "symbol": "BTC/USD", "side": "buy",
            "order_type": "market", "quantity": "0.01"})
    assert r.status_code == 200
    assert mgr.order is not None
    assert mgr.order.asset_class is AssetClass.CRYPTO
    assert mgr.order.symbol == "BTC/USD"


async def test_trade_autodetects_equity_for_plain_ticker() -> None:
    client, mgr = _client()
    async with client as c:
        r = await c.post("/api/trade", json={
            "symbol": "aapl", "side": "buy",
            "order_type": "market", "quantity": "10"})
    assert r.status_code == 200
    assert mgr.order is not None
    assert mgr.order.asset_class is AssetClass.EQUITY
    assert mgr.order.symbol == "AAPL"


async def test_trade_honors_explicit_asset_class_over_shape() -> None:
    # An explicit body value wins over the symbol-shape auto-detect: a plain
    # ticker sent with asset_class=crypto is tagged CRYPTO, proving the
    # auto-detect never clobbers an operator-supplied class.
    client, mgr = _client()
    async with client as c:
        r = await c.post("/api/trade", json={
            "symbol": "SPY", "side": "buy", "asset_class": "crypto",
            "order_type": "market", "quantity": "1"})
    assert r.status_code == 200
    assert mgr.order is not None
    assert mgr.order.asset_class is AssetClass.CRYPTO


async def test_trade_rejects_unsupported_crypto_pair_with_422() -> None:
    client, mgr = _client()
    async with client as c:
        r = await c.post("/api/trade", json={
            "symbol": "BTC/USDT", "side": "buy",
            "order_type": "market", "quantity": "0.01"})
    assert r.status_code == 422
    assert "BASE/USD" in r.json()["detail"]
    assert mgr.order is None  # never reached the broker/manager
