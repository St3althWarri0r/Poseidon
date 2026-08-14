"""A cap must never trap the operator in a position.

``risk/CLAUDE.md`` states the pairing this package depends on: most rules
return early for ``ctx.order.side.is_risk_reducing`` "so a loss halt or
exposure cap can never trap the operator in a losing position", and that is
safe only because ``ReduceOnlyRule`` is *not* exempted (an exit can never
exceed the position and flip the book short).

Two rules did not hold up their end of that contract, and both are reachable
by the ``PositionGuardian``'s stop-loss — the mechanism whose entire job is
limiting a loss:

* ``OrdersPerDayRule`` had no exemption, so once ``max_orders_per_day`` was
  reached every exit was refused until the Eastern-midnight counter roll.
  Retrying could not clear it.
* ``SlippageProtectionRule`` refused market orders on a wide or one-sided
  book, i.e. exactly the disorderly conditions a stop exists for.

These tests pin the exemptions. ``ReduceOnlyRule`` staying unexempted is
covered in ``test_risk.py`` and must stay that way.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from poseidon.core.errors import RiskViolation
from poseidon.core.models import Order, OrderSide, OrderType
from poseidon.risk.rules import OrdersPerDayRule, ReduceOnlyRule, SlippageProtectionRule

from .test_risk import ctx


def _exit(order_type: OrderType = OrderType.MARKET, limit: str | None = None) -> Order:
    return Order(
        symbol="AAPL", side=OrderSide.SELL, quantity=Decimal("100"),
        order_type=order_type,
        limit_price=Decimal(limit) if limit is not None else None,
        strategy="guardian",
    )


def _entry() -> Order:
    return Order(
        symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("100"),
        order_type=OrderType.MARKET, strategy="momentum",
    )


# -- OrdersPerDayRule ---------------------------------------------------------

def test_order_cap_never_blocks_an_exit() -> None:
    """At the cap, an exit must still pass — otherwise a position is trapped
    until Eastern midnight, guardian stop-losses included."""
    OrdersPerDayRule().check(ctx(_exit(), orders_today=10_000))


def test_order_cap_still_blocks_an_entry() -> None:
    """The cap must keep doing its job for orders that open risk."""
    with pytest.raises(RiskViolation, match="max_orders_per_day"):
        OrdersPerDayRule().check(ctx(_entry(), orders_today=10_000))


def test_order_cap_allows_entries_below_the_cap() -> None:
    OrdersPerDayRule().check(ctx(_entry(), orders_today=0))


# -- SlippageProtectionRule ---------------------------------------------------

def test_exit_gets_the_wider_band_on_a_market_order() -> None:
    """A 2% spread refuses an entry (1% band) but clears an exit (3% band)."""
    SlippageProtectionRule().check(ctx(_exit(), price="100.00", spread="2.00"))


def test_wide_spread_still_blocks_a_market_entry() -> None:
    with pytest.raises(RiskViolation, match="slippage_protection"):
        SlippageProtectionRule().check(ctx(_entry(), price="100.00", spread="2.00"))


def test_market_exit_beyond_even_the_exit_band_is_still_refused() -> None:
    """The exit band is still a bound: a genuinely broken book does not get an
    unbounded market fill. The guardian sends a marketable LIMIT instead —
    see test_guardian_stop_is_a_marketable_limit."""
    with pytest.raises(RiskViolation, match="slippage_protection"):
        SlippageProtectionRule().check(ctx(_exit(), price="100.00", spread="9.00"))


def test_exit_limit_may_price_through_the_book_within_the_exit_band() -> None:
    """A marketable limit — priced through the book so it crosses and fills —
    is allowed for an exit up to exit_slippage_multiple x the band."""
    # default band 1%, default multiple 3 -> up to 3% through the book
    SlippageProtectionRule().check(ctx(_exit(OrderType.LIMIT, "97.50"), price="100.00"))


def test_exit_limit_beyond_the_exit_band_is_still_refused() -> None:
    """The widened band is still a band: a fat-fingered exit is refused."""
    with pytest.raises(RiskViolation, match="slippage_protection"):
        SlippageProtectionRule().check(ctx(_exit(OrderType.LIMIT, "50.00"), price="100.00"))


def test_entry_limit_band_is_unchanged() -> None:
    """Entries keep the tight band — 2.5% away is still a fat finger."""
    with pytest.raises(RiskViolation, match="slippage_protection"):
        entry = Order(symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("100"),
                      order_type=OrderType.LIMIT, limit_price=Decimal("102.50"),
                      strategy="momentum")
        SlippageProtectionRule().check(ctx(entry, price="100.00"))


# -- the pairing that makes the above safe ------------------------------------

def test_reduce_only_is_still_not_exempt() -> None:
    """The exemptions above are only safe while ReduceOnlyRule still gates
    exits against the real position. Guard against anyone 'consistently'
    adding an is_risk_reducing early return to it."""
    import inspect
    source = inspect.getsource(ReduceOnlyRule)
    assert "is_risk_reducing" not in source, (
        "ReduceOnlyRule must never exempt risk-reducing orders — it is the rule "
        "that stops an 'exit' exceeding the position and flipping the book short. "
        "See risk/CLAUDE.md invariant 1."
    )
