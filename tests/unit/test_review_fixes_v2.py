"""Regression tests for the second adversarial review pass and the in-app
Schwab OAuth login flow. Each test pins one confirmed fix so it cannot
silently regress.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from poseidon.brokers.plugins.schwab import DEFAULT_REDIRECT_URI, SchwabBroker
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.errors import BrokerError
from poseidon.core.models import Order

_SCHWAB_CREDS = {"app_key": "k", "app_secret": "s", "refresh_token": "r", "account_hash": "h"}


# ---------------------------------------------------------------- Schwab paper guard (U11)

def test_schwab_rejects_paper_mode() -> None:
    # Schwab has no paper environment; a paper request must refuse rather than
    # silently trade live.
    with pytest.raises(BrokerError, match="no paper environment"):
        SchwabBroker(credentials=_SCHWAB_CREDS, paper=True)


def test_schwab_live_construction_ok() -> None:
    broker = SchwabBroker(credentials=_SCHWAB_CREDS, paper=False)
    assert broker.name == "schwab"
    assert broker.is_paper is False


# ---------------------------------------------------------------- Schwab OAuth login flow

def test_schwab_authorize_url_targets_login() -> None:
    url = SchwabBroker.authorize_url("APPKEY123")
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    assert "client_id=APPKEY123" in url
    assert "response_type=code" in url
    # Default callback matches the documented registered redirect URI.
    assert "127.0.0.1%3A8182" in url or DEFAULT_REDIRECT_URI in url.replace("%3A", ":")


def test_schwab_extract_code_from_redirect_url() -> None:
    pasted = "https://127.0.0.1:8182/?code=ABC.def-123&session=xyz"
    assert SchwabBroker.extract_code(pasted) == "ABC.def-123"


def test_schwab_extract_code_accepts_bare_code() -> None:
    assert SchwabBroker.extract_code("BARECODE123") == "BARECODE123"


def test_schwab_extract_code_rejects_garbage() -> None:
    with pytest.raises(BrokerError, match="no .*code"):
        SchwabBroker.extract_code("https://127.0.0.1:8182/?error=access_denied")


# ---------------------------------------------------------------- notional uses stop price (U3)

def test_estimated_notional_uses_stop_price_for_buy_stop() -> None:
    # A buy-stop above the market must be risk-checked at its trigger, not the
    # lower current price.
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.STOP,
                  quantity=Decimal("1000"), stop_price=Decimal("120"))
    assert order.estimated_notional(reference_price=Decimal("100")) == Decimal("120000")


def test_estimated_notional_prefers_highest_known_price() -> None:
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                  quantity=Decimal("10"), limit_price=Decimal("50"))
    # limit (50) beats reference (40)
    assert order.estimated_notional(reference_price=Decimal("40")) == Decimal("500")


def test_estimated_notional_none_without_any_price() -> None:
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                  quantity=Decimal("10"))
    assert order.estimated_notional() is None


# ---------------------------------------------------------------- ProposedTrade quantity guard (C6)

def test_proposed_trade_rejects_nonpositive_quantity() -> None:
    from pydantic import ValidationError

    from poseidon.core.models import ProposedTrade
    for bad in (Decimal("0"), Decimal("-5")):
        with pytest.raises(ValidationError):
            ProposedTrade(symbol="AAPL", side=OrderSide.BUY, asset_class=AssetClass.EQUITY,
                          quantity=bad)
