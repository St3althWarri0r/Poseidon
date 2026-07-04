"""Broker-plugin pure logic: status mapping and fill extraction."""

from __future__ import annotations

from decimal import Decimal

from poseidon.brokers.plugins.tastytrade import _STATUS_MAP, _extract_fills
from poseidon.core.enums import OrderStatus


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
