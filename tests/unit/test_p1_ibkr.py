"""F004 regression: IBKR ``cancel_order`` must adopt the broker's asynchronous
cancel state instead of hard-setting a terminal CANCELED with a zero fill.

An IBKR Client Portal cancel is asynchronous — the DELETE only *queues* the
request, and an in-flight fill can still land before it is processed. Before
commit 3a10e42 ``cancel_order`` unconditionally set ``OrderStatus.CANCELED``
with ``filled_quantity=0`` right after the DELETE. That recorded a last-moment
fill as a zero-fill cancel: a real live position left untracked/unguarded and a
false ``order.canceled`` audit entry, and — because CANCELED is terminal —
never re-polled after a restart. The fix re-polls ``order_status`` after the
DELETE and adopts the broker's authoritative state (fills included), falling
back to a NON-terminal ACCEPTED "cancel requested" only if that re-poll fails.

Harness mirrors ``tests/unit/test_brokers.py``: an ``httpx.MockTransport``
routed by path is injected as the broker's client (no network).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import httpx

from poseidon.brokers.plugins.ibkr import IBKRBroker
from poseidon.core.enums import OrderSide, OrderStatus, OrderType
from poseidon.core.models import Order


def _resting_order() -> Order:
    """A working (ACCEPTED) IBKR limit order the operator is cancelling."""
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        limit_price=Decimal("150.00"),
        broker="ibkr",
        broker_order_id="999",
        status=OrderStatus.ACCEPTED,
    )


def _ibkr(handler: Callable[[httpx.Request], httpx.Response]) -> IBKRBroker:
    """IBKRBroker whose HTTP client is a path-routed MockTransport. Construction
    does not connect, so we can swap the real verify-aware client for the mock."""
    broker = IBKRBroker(credentials={"account_id": "DU1234567"})
    broker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return broker


# F004 — cancel_order must RE-POLL order_status after the DELETE and ADOPT the
# broker's status/fills. The pre-fix code hard-set CANCELED with a 0 fill and
# never polled, silently dropping an in-flight fill into a false zero-fill cancel.
async def test_f004_cancel_adopts_inflight_fill() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            # IBKR only *queues* the cancel here; it is not yet terminal.
            return httpx.Response(200, json={"msg": "Request to cancel submitted"})
        if request.method == "GET" and "/order/status/" in request.url.path:
            # The order actually filled in-flight before the cancel processed.
            return httpx.Response(200, json={"order_status": "Filled", "cum_fill": "100"})
        return httpx.Response(404, json={})

    broker = _ibkr(handler)
    result = await broker.cancel_order(_resting_order())

    # The DELETE must be followed by an order_status re-poll (pre-fix never polled).
    assert any(m == "GET" and "/order/status/" in p for m, p in calls)
    # The authoritative broker state (a full fill) is adopted — not overwritten
    # with a terminal CANCELED that hides the fill and leaves the position untracked.
    assert result.status is OrderStatus.FILLED
    assert result.filled_quantity == Decimal("100")
    assert result.status is not OrderStatus.CANCELED


# F004 — when the post-DELETE order_status re-poll itself fails (BrokerError),
# cancel_order must fall back to a NON-terminal ACCEPTED "cancel requested"
# state, never the pre-fix hard CANCELED, so the order stays reconcilable and is
# re-polled after a restart instead of being frozen in a false terminal cancel.
async def test_f004_cancel_falls_back_to_accepted_when_status_poll_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"msg": "Request to cancel submitted"})
        # The status re-poll fails: a gateway 5xx becomes a BrokerError inside
        # order_status, which cancel_order must catch and fall back on.
        return httpx.Response(500, json={"error": "gateway busy"})

    broker = _ibkr(handler)
    result = await broker.cancel_order(_resting_order())

    assert result.status is OrderStatus.ACCEPTED
    assert result.status_reason == "cancel requested — awaiting broker confirmation"
    assert result.status is not OrderStatus.CANCELED
