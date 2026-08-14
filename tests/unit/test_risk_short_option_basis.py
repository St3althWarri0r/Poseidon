"""Short options are sized by strike basis, never premium.

``risk/CLAUDE.md`` invariant 2: a short option open is sized at
``strike x 100 x qty`` — the assignment/margin basis — because premium sizing
"understates capital at risk by orders of magnitude and lets a naked short slip
past buying-power, position, exposure, and leverage caps".

The multi-leg path was already well covered. The **single-leg** path was not:
mutating the contract multiplier at ``rules.py:128`` from ``Decimal(100)`` to
``Decimal(1)`` passed the entire suite — 1576 tests, zero failures. A naked
short AAPL 190 put would then size as $190 instead of $19,000 and clear every
cap that divides by notional.

These tests pin the multiplier and the no-premium-fallback rule so that line
cannot regress silently.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from poseidon.core.enums import AssetClass, OrderSide
from poseidon.core.errors import RiskViolation
from poseidon.core.models import OptionLeg, Order

from .test_risk import ctx

# AAPL, 2024-06-21 expiry, Put, strike 190.000 (trailing 8 digits = strike x 1000)
OCC_PUT_190 = "AAPL240621P00190000"


def _short_option(quantity: str = "1", symbol: str = OCC_PUT_190) -> Order:
    return Order(
        symbol=symbol, side=OrderSide.SELL_TO_OPEN, quantity=Decimal(quantity),
        asset_class=AssetClass.OPTION, strategy="cash_secured_puts",
    )


def test_single_leg_short_option_uses_the_full_contract_basis() -> None:
    """strike x 100 x qty — NOT strike x qty, and NOT the premium."""
    notional = ctx(_short_option(), price="3.50").notional
    assert notional == Decimal("19000"), (
        "a short 190 put must be sized at its $19,000 assignment basis; "
        f"got {notional}. If this is 190, the contract multiplier regressed."
    )


def test_short_option_basis_scales_with_quantity() -> None:
    assert ctx(_short_option("7"), price="3.50").notional == Decimal("133000")


def test_short_option_is_not_sized_by_premium() -> None:
    """The live quote is the premium. Notional must ignore it entirely — two
    very different premiums on the same contract size identically."""
    cheap = ctx(_short_option(), price="0.05").notional
    rich = ctx(_short_option(), price="45.00").notional
    assert cheap == rich == Decimal("19000")


def test_unparseable_occ_symbol_refuses_rather_than_falling_back() -> None:
    """risk/CLAUDE.md: 'If a short's strike can't be parsed from its OCC
    symbol, raise RiskViolation; never fall back to premium.'"""
    with pytest.raises(RiskViolation, match="strike"):
        _ = ctx(_short_option(symbol="AAPL-WEIRD"), price="3.50").notional


# -- multi-leg packages --------------------------------------------------------
#
# The single-leg branch above was the originally-reported gap. Mutation testing
# then showed the MULTI-LEG branch is the one with no coverage at all: skipping
# the `leg.side is not SELL_TO_OPEN` filter at rules.py:107 — so a package is
# priced at net-debit premium instead of its short legs' strike basis — passed
# the entire suite. On a 1-lot SPY 500/490 put credit spread at $3 net debit
# that is 300 instead of 50000: a 167x understatement, silent.

OCC_SPY_500P = "SPY240621P00500000"
OCC_SPY_490P = "SPY240621P00490000"


def _credit_spread(quantity: str = "1") -> Order:
    """Short the 500 put, long the 490 put — one lot."""
    return Order(
        symbol="SPY", side=OrderSide.SELL_TO_OPEN, quantity=Decimal(quantity),
        asset_class=AssetClass.OPTION, strategy="vertical_spreads",
        legs=[
            OptionLeg(contract_symbol=OCC_SPY_500P, side=OrderSide.SELL_TO_OPEN, quantity=1),
            OptionLeg(contract_symbol=OCC_SPY_490P, side=OrderSide.BUY_TO_OPEN, quantity=1),
        ],
    )


def test_multi_leg_package_is_sized_at_its_short_legs_strike_basis() -> None:
    """500 x 100 x 1 = 50,000 — the SHORT leg's assignment basis, not the
    package's net debit. Nothing here verifies the long leg covers the short,
    so short legs are deliberately over-sized (fail safe)."""
    notional = ctx(_credit_spread(), price="3.00").notional
    assert notional == Decimal("50000"), (
        f"a 500/490 put spread must size at the short leg's $50,000 basis; got {notional}. "
        "If this is 300, the SELL_TO_OPEN leg filter regressed and the package "
        "is being sized by premium."
    )


def test_multi_leg_ignores_the_quoted_premium_entirely() -> None:
    cheap = ctx(_credit_spread(), price="0.10").notional
    rich = ctx(_credit_spread(), price="40.00").notional
    assert cheap == rich == Decimal("50000")


def test_multi_leg_sums_every_short_leg() -> None:
    """Two short legs are additive — the package is over-sized on purpose."""
    order = Order(
        symbol="SPY", side=OrderSide.SELL_TO_OPEN, quantity=Decimal("1"),
        asset_class=AssetClass.OPTION, strategy="iron_condors",
        legs=[
            OptionLeg(contract_symbol=OCC_SPY_500P, side=OrderSide.SELL_TO_OPEN, quantity=1),
            OptionLeg(contract_symbol=OCC_SPY_490P, side=OrderSide.SELL_TO_OPEN, quantity=1),
        ],
    )
    assert ctx(order, price="3.00").notional == Decimal("99000")  # (500 + 490) x 100


def test_long_only_package_keeps_premium_sizing() -> None:
    """No short legs -> no assignment basis -> net-debit premium is correct."""
    order = Order(
        symbol="SPY", side=OrderSide.BUY_TO_OPEN, quantity=Decimal("2"),
        asset_class=AssetClass.OPTION, strategy="protective_puts",
        legs=[
            OptionLeg(contract_symbol=OCC_SPY_500P, side=OrderSide.BUY_TO_OPEN, quantity=1),
            OptionLeg(contract_symbol=OCC_SPY_490P, side=OrderSide.BUY_TO_OPEN, quantity=1),
        ],
    )
    assert ctx(order, price="3.00").notional == Decimal("600")  # 3.00 x 100 x 2


def test_multi_leg_unparseable_short_leg_refuses() -> None:
    order = Order(
        symbol="SPY", side=OrderSide.SELL_TO_OPEN, quantity=Decimal("1"),
        asset_class=AssetClass.OPTION, strategy="vertical_spreads",
        legs=[OptionLeg(contract_symbol="SPY-BROKEN", side=OrderSide.SELL_TO_OPEN, quantity=1)],
    )
    with pytest.raises(RiskViolation, match="strike"):
        _ = ctx(order, price="3.00").notional


def test_long_option_still_uses_premium_times_multiplier() -> None:
    """Only SELL_TO_OPEN switches to strike basis. A long option's capital at
    risk really is the premium — sized at premium x 100 x qty."""
    long_call = Order(
        symbol=OCC_PUT_190, side=OrderSide.BUY_TO_OPEN, quantity=Decimal("2"),
        asset_class=AssetClass.OPTION, strategy="protective_puts",
    )
    assert ctx(long_call, price="3.50").notional == Decimal("700")
