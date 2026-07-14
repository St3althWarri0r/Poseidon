"""Broker-plugin pure logic: status mapping and fill extraction."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from poseidon.brokers.base import Broker
from poseidon.brokers.plugins.tastytrade import _STATUS_MAP, _extract_fills
from poseidon.core.enums import BrokerCapability, OrderStatus
from poseidon.core.errors import BrokerError
from poseidon.core.models import AccountSnapshot, Order, Position


def test_partially_removed_is_terminal_cancelled() -> None:
    # F5 regression: "Partially Removed" means the order was cancelled after a
    # partial fill — a TERMINAL state. Mapping it to PARTIALLY_FILLED (which is
    # not terminal) would spin the poll loop forever and keep it in open_orders.
    status = _STATUS_MAP["Partially Removed"]
    assert status is OrderStatus.CANCELED
    assert status.is_terminal


def test_extract_fills_aggregates_leg_fills() -> None:
    # Quantity-weighted average across all legs' fills.
    row = {
        "legs": [
            {"fills": [{"quantity": 3, "fill-price": "10.00"},
                       {"quantity": 2, "fill-price": "12.50"}]},
            {"fills": [{"quantity": 5, "fill-price": "11.00"}]},
        ]
    }
    qty, avg = _extract_fills(row)
    assert qty == Decimal("10")
    # (3*10 + 2*12.5 + 5*11) / 10 = (30 + 25 + 55) / 10 = 11.0
    assert avg == Decimal("11.0")


def test_extract_fills_none_when_unfilled() -> None:
    assert _extract_fills({"legs": [{"fills": []}]}) == (Decimal(0), None)
    assert _extract_fills({}) == (Decimal(0), None)


# ---- _request idempotency on an unparseable 2xx body (F003 regression) ----
# A 2xx whose body fails to parse is a POST-SEND failure: the broker already
# received the request. On a non-idempotent submit it must be raised ambiguous
# + non-retryable (never auto-resubmitted) exactly like a post-send timeout /
# 5xx, or the order manager double-fills at brokers with no idempotency key.

class _BareBroker(Broker):
    name = "bare"

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset()

    async def connect(self) -> None: ...
    async def account(self) -> AccountSnapshot: ...  # type: ignore[empty-body]
    async def positions(self) -> list[Position]:
        return []

    async def submit_order(self, order: Order) -> Order:
        return order

    async def cancel_order(self, order: Order) -> Order:
        return order

    async def order_status(self, order: Order) -> Order:
        return order

    async def open_orders(self) -> list[Order]:
        return []


def _broker_returning(body: bytes, status: int = 200) -> _BareBroker:
    broker = _BareBroker(credentials={})
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(status, content=body, headers={"content-type": "application/json"})
    )
    broker._client = httpx.AsyncClient(transport=transport)
    return broker


async def test_request_unparseable_2xx_on_nonidempotent_is_ambiguous() -> None:
    broker = _broker_returning(b"<html>maintenance</html>")
    with pytest.raises(BrokerError) as exc:
        await broker._request("POST", "http://x/order", idempotent=False)
    # Non-idempotent post-send failure: outcome unknown -> must NOT be resubmitted.
    assert exc.value.ambiguous is True
    assert exc.value.retryable is False


async def test_request_unparseable_2xx_on_idempotent_stays_retryable() -> None:
    broker = _broker_returning(b"not json")
    with pytest.raises(BrokerError) as exc:
        await broker._request("GET", "http://x/account", idempotent=True)
    # Idempotent read: safe to retry, not ambiguous (mirrors timeout/5xx branch).
    assert exc.value.retryable is True
    assert exc.value.ambiguous is False
