"""Regression pins for tradier.py findings F008, F009, F010 (fixed in 3a10e42).

Each test fails on the pre-fix code and passes on the fixed source; the per-test
comment names the finding and the exact defect it guards. No network: the
broker's HTTP client is swapped for an ``httpx.MockTransport``, mirroring
tests/unit/test_brokers.py.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from poseidon.brokers.plugins.tradier import TradierBroker
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.errors import BrokerError
from poseidon.core.models import Order

_CREDS = {"access_token": "test-token", "account_id": "ACCT-1"}


def _tradier(handler: Any) -> TradierBroker:
    """A TradierBroker whose HTTP client is backed by a MockTransport handler,
    so account()/positions()/submit_order() never touch the network."""
    broker = TradierBroker(credentials=_CREDS, paper=True)
    broker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return broker


# F008 — a legitimately-zero stock_buying_power (a maxed-out margin account) must
# NOT fall through to cash_available/total_cash. The old `or` chain treated 0 as
# missing and over-reported buying power to BuyingPowerRule (a false risk pass).
async def test_f008_zero_stock_buying_power_not_overridden_by_cash() -> None:
    payload = {
        "balances": {
            "total_equity": 50000,
            "total_cash": 20000,
            "margin": {"stock_buying_power": 0},
            "cash": {"cash_available": 20000},
        }
    }
    broker = _tradier(lambda _req: httpx.Response(200, json=payload))
    snap = await broker.account()
    # Pre-fix: `0 or 20000 or ...` -> Decimal("20000"). Fixed: first present
    # (not-None) field wins, so the valid 0 is kept -> Decimal("0").
    assert snap.buying_power == Decimal("0")


# F009 — Tradier cost_basis is the TOTAL dollar cost (already x100 per contract).
# avg_entry_price must be the per-share premium (cost/qty/100); otherwise option
# exposure reads 100x too high once _position_notional re-applies the x100 mult.
async def test_f009_option_avg_entry_price_is_per_share_premium() -> None:
    occ = "SPY250117C00500000"  # len 18 > 12 -> classified as an OPTION position
    assert len(occ) > 12
    payload = {
        "positions": {
            "position": {"symbol": occ, "cost_basis": 500, "quantity": 2},
        }
    }
    broker = _tradier(lambda _req: httpx.Response(200, json=payload))
    positions = await broker.positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.asset_class is AssetClass.OPTION
    # Pre-fix: 500/2 = Decimal("250") (per-contract cost). Fixed: /100 -> the
    # per-share premium Decimal("2.5").
    assert pos.avg_entry_price == Decimal("2.5")


# F010 — a digit-less option symbol made a bare next() raise StopIteration (not a
# BrokerError). StopIteration escapes the manager's BrokerError-only submit
# handler and aborts the review cycle; the parse must reject it as a BrokerError,
# and must do so BEFORE any HTTP call (the _no_http transport asserts on any request).
async def test_f010_digitless_option_symbol_raises_broker_error() -> None:
    def _no_http(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("digit-less option symbol must be rejected before any HTTP call")

    broker = _tradier(_no_http)
    order = Order(
        symbol="AAPL",  # no digit -> pre-fix `next(...)` raises StopIteration
        asset_class=AssetClass.OPTION,
        side=OrderSide.BUY_TO_OPEN,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
    )
    with pytest.raises(BrokerError) as exc:
        await broker.submit_order(order)
    # A malformed symbol is a definitive rejection, never auto-retried.
    assert exc.value.retryable is False
