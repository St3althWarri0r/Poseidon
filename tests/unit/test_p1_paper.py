"""Regression pins for the paper broker persistence hardening (F005, F006).

Landed in commit 3a10e42. Kept in a dedicated file (test_p1_paper.py) so it
never collides with the existing paper-broker suites. Each test is written to
FAIL on the pre-fix source (where the raw OS/JSON exception escapes) and PASS
on the fix.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from poseidon.brokers.plugins.paper import PaperBroker
from poseidon.core.enums import OrderSide, OrderStatus, OrderType
from poseidon.core.errors import BrokerError
from poseidon.core.models import Order

from ..conftest import make_quote


# F006 — a corrupt/truncated state file must surface as BrokerError, not a raw
# json.JSONDecodeError. Guards invariant #5 (errors subclass PoseidonError):
# pre-fix connect() -> _load_state() called an unguarded json.loads(), leaking a
# JSONDecodeError that the kernel / OrderManager (PoseidonError-only handling)
# cannot classify.
async def test_f006_corrupt_state_file_raises_broker_error(tmp_path) -> None:
    state_file = tmp_path / "paper.json"
    # Syntactically MALFORMED JSON (a torn/truncated prior write). It must trip
    # the json.loads guard — not sail past it and die later at an unguarded
    # Decimal()/model_validate on a well-formed-but-wrong value.
    state_file.write_text('{"cash": "100000", "positions":', encoding="utf-8")

    async def qf(symbol: str):
        return make_quote(symbol, "100.00")

    broker = PaperBroker(
        credentials={}, options={"quote_fn": qf, "state_file": str(state_file)}
    )

    # json.JSONDecodeError is a ValueError and shares no ancestry with
    # BrokerError, so pytest.raises(BrokerError) does NOT swallow the pre-fix
    # exception — it propagates out of connect() and fails the test. On the fix,
    # _load_state re-raises BrokerError(..., "corrupt paper state file ...").
    with pytest.raises(BrokerError, match="corrupt"):
        await broker.connect()


# F005 — a _save_state() OSError (ENOSPC / permission) that hits mid-fill must be
# swallowed so the authoritative in-memory book stays consistent: the fill still
# applies and submit_order does NOT raise. Pre-fix the raw OSError escaped
# _apply_fill -> submit_order's `except Exception` popped the order and re-raised,
# corrupting the book (cash/position already mutated, order gone) AND crashing the
# review cycle with a non-BrokerError the manager does not catch.
async def test_f005_save_failure_does_not_abort_or_corrupt_fill(
    tmp_path, monkeypatch
) -> None:
    async def qf(symbol: str):
        return make_quote(symbol, "190.00")  # bid 189.95 / ask 190.05

    broker = PaperBroker(
        credentials={},
        options={"quote_fn": qf, "state_file": str(tmp_path / "paper.json")},
    )
    await broker.connect()

    # Force the atomic-replace step of _save_state to fail like a full disk. The
    # sentinel proves the OSError path was genuinely exercised — without it a
    # mis-fired monkeypatch would leave a hollow pin that greens on both the
    # pre- and post-fix source and pins nothing.
    fired = {"n": 0}

    def _boom(*args: object, **kwargs: object) -> None:
        fired["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", _boom)

    market_buy = Order(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=Decimal("10"),
    )
    # No try/except: on the pre-fix source this call raises OSError and the test
    # fails right here.
    order = await broker.submit_order(market_buy)

    assert fired["n"] >= 1, "the state-save OSError path was never exercised"
    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price == Decimal("190.05")  # filled at the ask

    positions = await broker.positions()
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("10")

    # Cash debited exactly once — the book applied the fill and did not roll it
    # back or double-count it despite the persistence failure.
    account = await broker.account()
    assert account.cash == Decimal("98099.50")  # 100000 - 10 * 190.05
