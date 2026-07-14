"""Regression test pinning F002 — ProposedTrade must reject price-less
limit/stop orders at construction time.

F002 (core/models.py): a LIMIT / STOP_LIMIT ``ProposedTrade`` built with
``limit_price=None`` (or a STOP / STOP_LIMIT with ``stop_price=None``) used to
construct cleanly. That price-less "limit" order then slips through every risk
rule untouched — SlippageProtectionRule only bands an order whose ``limit_price``
is not None — and the paper broker fills it AT MARKET, defeating the fat-finger
guard and the SYSTEM_PROMPT's "limit orders only, priced from the live quote"
guarantee. The fix is a pydantic ``model_validator(mode="after")`` that rejects
the inconsistent (order_type, price) combinations up front, voiding the whole
decision rather than executing a mispriced coupled leg.

Each "rejected" test constructs cleanly on the PRE-FIX model (verified against
commit 3a10e42^) and raises ``ValidationError`` on the fixed model, so it truly
exercises the added validator. The two "allowed" tests are negative controls:
they pin that the guard is narrow (a valid MARKET / priced-LIMIT order still
constructs) so a future over-broad guard is also caught.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from poseidon.core.enums import OrderSide, OrderType
from poseidon.core.models import ProposedTrade


def _trade(
    order_type: OrderType,
    *,
    limit_price: Decimal | None = None,
    stop_price: Decimal | None = None,
) -> ProposedTrade:
    # symbol / side / quantity are ProposedTrade's only required fields; hold
    # them fixed so each test varies ONLY the order_type + price under scrutiny.
    return ProposedTrade(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal("100"),
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
    )


# F002: a LIMIT order with no limit_price must be rejected — otherwise it reaches
# the broker and fills AT MARKET with zero price protection (pre-fix: constructs).
def test_f002_limit_without_limit_price_rejected() -> None:
    with pytest.raises(ValidationError, match="limit_price"):
        _trade(OrderType.LIMIT, limit_price=None)


# F002: STOP_LIMIT with no limit_price is the same fat-finger hole as a bare
# LIMIT; stop_price is supplied so only the missing-limit_price guard can fire.
def test_f002_stop_limit_without_limit_price_rejected() -> None:
    with pytest.raises(ValidationError, match="limit_price"):
        _trade(OrderType.STOP_LIMIT, limit_price=None, stop_price=Decimal("90"))


# F002: a STOP order with no stop_price falls through the paper broker to an
# immediate market fill; the guard must reject it at construction (pre-fix: none).
def test_f002_stop_without_stop_price_rejected() -> None:
    with pytest.raises(ValidationError, match="stop_price"):
        _trade(OrderType.STOP, stop_price=None)


# F002: STOP_LIMIT with no stop_price; limit_price is supplied so only the
# missing-stop_price guard can fire (isolates the second validator branch).
def test_f002_stop_limit_without_stop_price_rejected() -> None:
    with pytest.raises(ValidationError, match="stop_price"):
        _trade(OrderType.STOP_LIMIT, limit_price=Decimal("100"), stop_price=None)


# F002 (negative control): a MARKET order legitimately carries no prices and must
# still construct — the guard must not over-reject valid market orders.
def test_f002_market_without_prices_allowed() -> None:
    trade = _trade(OrderType.MARKET)
    assert trade.order_type is OrderType.MARKET
    assert trade.limit_price is None
    assert trade.stop_price is None


# F002 (negative control): a properly priced LIMIT order must construct — the
# guard rejects only the missing-price case, not every limit order.
def test_f002_limit_with_limit_price_allowed() -> None:
    trade = _trade(OrderType.LIMIT, limit_price=Decimal("100"))
    assert trade.order_type is OrderType.LIMIT
    assert trade.limit_price == Decimal("100")
