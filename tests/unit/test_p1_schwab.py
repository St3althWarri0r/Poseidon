"""Part-1 sweep regression tests for the Charles Schwab broker plugin.

Pins one confirmed adversarial-review fix (commit 3a10e42) on
``src/poseidon/brokers/plugins/schwab.py`` so it cannot silently regress.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from poseidon.brokers.plugins.schwab import SchwabBroker
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.errors import BrokerError
from poseidon.core.models import OptionLeg, Order

_SCHWAB_CREDS = {"app_key": "k", "app_secret": "s", "refresh_token": "r", "account_hash": "h"}


# ---------------------------------------------------------------- F007 multi-leg guard

# F007: submit_order only builds a SINGLE-leg orderStrategyType. Pre-fix it
# silently dropped order.legs and POSTed one leg on order.symbol (the underlying),
# executing a naked position whose real risk differs from the vetted spread. The
# guard must reject a legs-bearing order LOUDLY, on the first line, before any HTTP.
async def test_f007_multileg_order_rejected_not_silently_dropped() -> None:
    broker = SchwabBroker(credentials=_SCHWAB_CREDS, paper=False)
    # A defined-risk vertical call spread: two legs the plugin cannot represent.
    # Pre-fix these were discarded and only order.symbol ("AAPL") was submitted.
    spread = Order(
        symbol="AAPL",
        asset_class=AssetClass.OPTION,
        side=OrderSide.BUY_TO_OPEN,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("1.50"),
        legs=[
            OptionLeg(contract_symbol="AAPL240621C00190000", side=OrderSide.BUY_TO_OPEN, quantity=1),
            OptionLeg(contract_symbol="AAPL240621C00200000", side=OrderSide.SELL_TO_OPEN, quantity=1),
        ],
    )
    # The guard raises on the first line, before _auth_headers/HTTP, so this needs
    # no network. Pre-fix code fell through to the OAuth token refresh and raised a
    # different (network/auth) BrokerError whose message never mentions multi-leg.
    with pytest.raises(BrokerError, match="does not support multi-leg") as excinfo:
        await broker.submit_order(spread)
    # Never auto-resubmitted: a spread the plugin cannot build is a hard reject,
    # unlike the retryable connect error the pre-fix network path would surface.
    assert excinfo.value.retryable is False
