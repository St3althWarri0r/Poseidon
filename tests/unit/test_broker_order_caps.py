"""Broker-side per-order caps: declared by the broker, enforced at preflight,
and surfaced to the AI through get_risk_status.

Alpaca refuses any single crypto order above $200k notional (HTTP 403, code
40310000 — observed live 2026-07-20 on a $5.76M UNI/USD buy). Poseidon's own
rails are portfolio-relative and correctly passed that order, so without this
seam the AI re-proposes the same oversized trade every cycle and the broker
rejects it late, noisily, and unexplained.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from poseidon.ai.tools import ToolDispatcher
from poseidon.brokers.plugins.alpaca import AlpacaBroker
from poseidon.brokers.plugins.paper import PaperBroker
from poseidon.core.clock import FreshnessPolicy
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.models import Order
from poseidon.data.router import DataRouter
from poseidon.portfolio.state import PortfolioState

from ..conftest import FakeProvider

_CREDS = {"key_id": "k", "secret_key": "s"}


def _crypto_order(qty: str, limit: str | None = None, *,
                  order_type: OrderType = OrderType.LIMIT) -> Order:
    return Order(symbol="UNI/USD", asset_class=AssetClass.CRYPTO, side=OrderSide.BUY,
                 order_type=order_type, quantity=Decimal(qty),
                 limit_price=Decimal(limit) if limit else None)


async def test_preflight_rejects_crypto_over_cap_with_remedy() -> None:
    # The exact live failure: ~$5.76M of UNI/USD. The reason must name the cap
    # AND the remedy, because it feeds the audit chain and the notification.
    broker = AlpacaBroker(credentials=_CREDS)
    reason = await broker.preflight(_crypto_order("1585661", "3.635"))
    assert reason is not None
    assert "200,000" in reason
    assert "per order" in reason
    assert "cycle" in reason  # remedy: size under the cap, build across cycles


async def test_preflight_allows_crypto_at_cap() -> None:
    broker = AlpacaBroker(credentials=_CREDS)
    assert await broker.preflight(_crypto_order("1", "200000")) is None


async def test_preflight_never_false_rejects() -> None:
    broker = AlpacaBroker(credentials=_CREDS)
    # Equities: no alpaca-side cap is verified, so none is hardcoded.
    equity = Order(symbol="TQQQ", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                   quantity=Decimal("100000"), limit_price=Decimal("85"))
    assert await broker.preflight(equity) is None
    # Unpriceable crypto MARKET order: cannot verify the notional — the base
    # contract forbids converting a knowledge gap into a false rejection.
    assert await broker.preflight(_crypto_order("999999", order_type=OrderType.MARKET)) is None


async def test_preflight_prices_market_orders_from_arrival() -> None:
    broker = AlpacaBroker(credentials=_CREDS)
    order = _crypto_order("100", order_type=OrderType.MARKET)
    order.arrival_price = Decimal("3000")  # stamped by validate_order (TCA benchmark)
    reason = await broker.preflight(order)
    assert reason is not None  # $300k > cap


def test_alpaca_declares_its_crypto_cap() -> None:
    limits = AlpacaBroker(credentials=_CREDS).order_limits()
    assert limits["max_order_notional"]["crypto"] == "200000"


def test_base_brokers_declare_no_limits(tmp_path) -> None:
    broker = PaperBroker(credentials={}, options={
        "starting_cash": "1000", "state_file": str(tmp_path / "p.json")})
    assert broker.order_limits() == {}


class _Risk:
    def status(self) -> dict[str, Any]:
        return {"orders_today": 0}


def _dispatcher(**kwargs: Any) -> ToolDispatcher:
    router = DataRouter([(FakeProvider(), 10)], FreshnessPolicy())
    return ToolDispatcher(router, PortfolioState(), _Risk(),  # type: ignore[arg-type]
                          allow_delayed_quotes=True, **kwargs)


async def test_risk_status_tool_surfaces_broker_limits() -> None:
    # The model can only size within a cap it can SEE: get_risk_status carries
    # the active broker's declared per-order limits, read through a callable so
    # a broker hot-swap is reflected without rebuilding dispatchers.
    caps = {"max_order_notional": {"crypto": "200000"}}
    d = _dispatcher(broker_limits=lambda: caps)
    out, is_error = await d.dispatch("get_risk_status", {})
    assert not is_error
    assert '"broker_limits"' in out
    assert '"200000"' in out


async def test_risk_status_tool_unchanged_without_broker_limits() -> None:
    # Constructions that don't pass the callable (older call sites, tests)
    # keep the exact old output shape.
    d = _dispatcher()
    out, is_error = await d.dispatch("get_risk_status", {})
    assert not is_error
    assert "broker_limits" not in out


async def test_preflight_prices_limit_orders_off_the_limit_price() -> None:
    # Alpaca computes the crypto cap off the LIMIT price (proven from live 403
    # bodies: notional == qty x limit to the cent), so a resting buy below the
    # mid — or an exit whose limit sits under a higher arrival mid — is
    # PLACEABLE at <=$200k and must not be falsely refused. Conservative
    # max(limit, arrival) pricing was the original defect here.
    broker = AlpacaBroker(credentials=_CREDS)
    buy = _crypto_order("60000", "3.30")          # $198,000 at the limit
    buy.arrival_price = Decimal("3.40")           # mid above the limit: irrelevant
    assert await broker.preflight(buy) is None
    exit_order = Order(symbol="BTC/USD", asset_class=AssetClass.CRYPTO,
                       side=OrderSide.SELL, order_type=OrderType.LIMIT,
                       quantity=Decimal("1.75"), limit_price=Decimal("114000"))
    exit_order.arrival_price = Decimal("115000")  # $199,500 at the limit: placeable
    assert await broker.preflight(exit_order) is None


async def test_risk_status_tool_reads_broker_limits_per_call() -> None:
    # The callable is read PER DISPATCH, never cached at construction: a broker
    # hot-swap (switch_broker rebinds kernel.broker without rebuilding
    # dispatchers) must be reflected on the very next cycle.
    limits = {"max_order_notional": {"crypto": "200000"}}
    d = _dispatcher(broker_limits=lambda: limits)
    out, _ = await d.dispatch("get_risk_status", {})
    assert '"200000"' in out
    limits = {"max_order_notional": {"crypto": "50000"}}
    out, _ = await d.dispatch("get_risk_status", {})
    assert '"50000"' in out
    assert '"200000"' not in out


def test_app_wires_broker_limits_as_live_lambda() -> None:
    # Wiring pin (AST, per the test_snapshot_wiring precedent): BOTH dispatcher
    # constructions in the kernel pass broker_limits as a LAMBDA that
    # dereferences self.broker at call time. A bound method
    # (self.broker.order_limits) type-checks identically but pins the
    # pre-switch broker forever; dropping the kwarg blinds the AI to caps.
    import ast
    import inspect

    from poseidon import app as app_module

    tree = ast.parse(inspect.getsource(app_module))
    sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "ToolDispatcher"
    ]
    assert len(sites) == 2, "expected exactly the cycle + chat dispatcher constructions"
    for call in sites:
        kw = next((k for k in call.keywords if k.arg == "broker_limits"), None)
        assert kw is not None, "dispatcher construction lost the broker_limits kwarg"
        assert isinstance(kw.value, ast.Lambda), "broker_limits must be a lambda (late binding)"
        derefs_broker = any(
            isinstance(n, ast.Attribute) and n.attr == "broker"
            and isinstance(n.value, ast.Name) and n.value.id == "self"
            for n in ast.walk(kw.value.body)
        )
        assert derefs_broker, "the lambda must read self.broker at CALL time"
