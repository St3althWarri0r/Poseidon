"""In-flight exposure reservations (F021): a notional is STAGED at validation
time (not just at submit), so two genuinely concurrent pipelines each see the
other's staged exposure and cannot both pass the gross/leverage/exposure caps
against a snapshot that reflects neither. A validated-but-rejected order releases
its stash; approval-mode re-validation never counts an order against itself.

These drive RiskEngine.validate_order directly over a FakeProvider so the caps
(not I/O) are the only constraint under test. asyncio auto mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from poseidon.core.clock import FreshnessPolicy, MarketClock, MarketSession
from poseidon.core.config import RiskConfig
from poseidon.core.enums import OrderSide, OrderType
from poseidon.core.errors import RiskViolation
from poseidon.core.events import EventBus
from poseidon.core.models import AccountSnapshot, Order, Position
from poseidon.data.router import DataRouter
from poseidon.portfolio.state import PortfolioState
from poseidon.risk.engine import RiskEngine

from ..conftest import FakeProvider

NOW = datetime.now(UTC)


def _engine() -> RiskEngine:
    bus = EventBus()
    router = DataRouter([(FakeProvider(name="feed", price="100"), 10)], FreshnessPolicy())
    portfolio = PortfolioState()
    portfolio.account = AccountSnapshot(
        broker="paper", account_id="t", equity=Decimal("100000"),
        cash=Decimal("100000"), buying_power=Decimal("500000"), as_of=NOW,
    )
    # One held position worth $150k -> gross_exposure() == 150k, so with a 200k
    # cap (2.0x leverage on $100k equity) there is exactly $50k of headroom.
    portfolio.positions = [Position(symbol="HELD", quantity=Decimal("1500"),
                                    avg_entry_price=Decimal("100"), as_of=NOW)]
    portfolio.synced_at = NOW
    # Raise every OTHER cap so gross/leverage is the sole binding constraint.
    config = RiskConfig(
        max_leverage=2.0, max_portfolio_exposure_pct=2.0, max_position_pct=1.0,
        max_order_notional=Decimal("100000"), news_blackout_minutes_before_econ=0,
    )
    return RiskEngine(config, portfolio, router, MarketClock(), bus)


def _buy(symbol: str, qty: str = "400") -> Order:
    # 400 x $100 = $40k notional; two of them ($80k) exceed the $50k headroom.
    return Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.LIMIT,
                 quantity=Decimal(qty), limit_price=Decimal("100"))


async def test_f021_concurrent_validations_cannot_stack_past_caps() -> None:
    risk = _engine()
    with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
        # A ($40k) validates: 150k + 40k = 190k < 200k -> passes and STAGES 40k.
        await risk.validate_order(_buy("AAA"))
        # B ($40k) validates BEFORE A submits. Post-fix it sees A's staged 40k:
        # 150k + 40k + 40k = 230k > 200k -> RiskViolation. Pre-fix pending is empty
        # (A has not reached note_order_submitted) so B wrongly passes and the two
        # together breach the 2.0x cap.
        with pytest.raises(RiskViolation):
            await risk.validate_order(_buy("BBB"))


async def test_f021_revalidation_does_not_count_own_stash() -> None:
    risk = _engine()
    with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
        order = _buy("AAA", qty="500")  # $50k, exactly the headroom
        await risk.validate_order(order)  # 150k + 50k = 200k -> passes, stages 50k
        # Approval mode re-validates the SAME order after the human approves. It
        # must exclude its own staged 50k, or it self-rejects (150k+50k+50k=250k).
        await risk.validate_order(order)  # still 150k + 50k = 200k -> passes


async def test_f021_release_validated_lifts_the_reservation() -> None:
    risk = _engine()
    with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
        a = _buy("AAA")
        await risk.validate_order(a)  # stages 40k -> would block a second 40k order
        # A order rejected before submission must release its stash, or it
        # over-counts exposure against later orders until the 15-min prune.
        risk.release_validated(a.id)
        await risk.validate_order(_buy("BBB"))  # 150k + 0 + 40k = 190k -> passes


async def test_f021_risk_reducing_exit_does_not_reserve_exposure() -> None:
    risk = _engine()
    with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
        # A risk-reducing SELL of part of the $150k held position validates. It
        # shrinks the book, so it must NOT stage exposure — or a concurrent open
        # would see a phantom +$50k of pending.
        sell = Order(symbol="HELD", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                     quantity=Decimal("500"), limit_price=Decimal("100"))  # $50k exit
        await risk.validate_order(sell)  # reduce-only passes (held 1500 >= 500)
        # A $50k BUY must still pass: 150k + 0 + 50k = 200k <= cap. If the exit had
        # wrongly reserved, it would be 150k + 50k + 50k = 250k > 200k and reject.
        await risk.validate_order(_buy("AAA", qty="500"))
