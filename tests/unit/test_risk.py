"""Risk rules, circuit breaker, and cooldowns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_trader.core.clock import MarketClock
from aegis_trader.core.config import RiskConfig
from aegis_trader.core.enums import OrderSide, OrderType
from aegis_trader.core.errors import RiskViolation
from aegis_trader.core.models import AccountSnapshot, Bar, EconomicEvent, Order, Position
from aegis_trader.portfolio.state import PortfolioState
from aegis_trader.risk.circuit import CircuitBreaker, TradeCooldowns
from aegis_trader.risk.rules import (
    BuyingPowerRule,
    CooldownRule,
    DailyLossRule,
    EconBlackoutRule,
    FreshPortfolioRule,
    OrderNotionalRule,
    PositionSizeRule,
    RiskContext,
    SlippageProtectionRule,
    SpreadRule,
    VolumeRule,
)

from ..conftest import make_quote


def portfolio_with(equity: str = "100000", cash: str = "50000") -> PortfolioState:
    state = PortfolioState()
    state.account = AccountSnapshot(
        broker="paper", account_id="t", equity=Decimal(equity),
        cash=Decimal(cash), buying_power=Decimal(cash), as_of=datetime.now(UTC),
    )
    state.synced_at = datetime.now(UTC)
    return state


def ctx(order: Order, *, portfolio: PortfolioState | None = None, price: str = "100.00",
        spread: str = "0.10", bars: list[Bar] | None = None,
        econ: list[EconomicEvent] | None = None, orders_today: int = 0,
        cooldown: float = 0.0) -> RiskContext:
    return RiskContext(
        order=order,
        quote=make_quote(order.symbol, price, spread=spread),
        portfolio=portfolio or portfolio_with(),
        config=RiskConfig(),
        clock=MarketClock(),
        recent_bars=bars if bars is not None else _bars(),
        upcoming_econ=econ or [],
        orders_today=orders_today,
        cooldown_remaining=cooldown,
    )


def _bars(volume: int = 500_000, count: int = 25) -> list[Bar]:
    now = datetime.now(UTC)
    return [
        Bar(symbol="AAPL", open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100"), volume=volume, start=now - timedelta(days=count - i),
            end=now - timedelta(days=count - i), source="t")
        for i in range(count)
    ]


def buy(qty: str = "10", limit: str = "100.00") -> Order:
    return Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                 quantity=Decimal(qty), limit_price=Decimal(limit))


class TestRules:
    def test_fresh_portfolio_blocks_stale_state(self) -> None:
        state = portfolio_with()
        state.synced_at = datetime.now(UTC) - timedelta(minutes=10)
        with pytest.raises(RiskViolation, match="fresh_portfolio_state"):
            FreshPortfolioRule().check(ctx(buy(), portfolio=state))

    def test_buying_power(self) -> None:
        state = portfolio_with(cash="500")
        with pytest.raises(RiskViolation, match="buying_power"):
            BuyingPowerRule().check(ctx(buy("10"), portfolio=state))
        BuyingPowerRule().check(ctx(buy("4"), portfolio=state))  # 400 <= 500 OK

    def test_position_size_cap(self) -> None:
        # 10% of 100k = 10k cap; 150 * 100 = 15k breaches.
        with pytest.raises(RiskViolation, match="max_position_size"):
            PositionSizeRule().check(ctx(buy("150")))
        PositionSizeRule().check(ctx(buy("50")))

    def test_position_size_includes_existing(self) -> None:
        state = portfolio_with()
        state.positions = [
            Position(symbol="AAPL", quantity=Decimal("80"), avg_entry_price=Decimal("100"),
                     market_value=Decimal("8000"), broker="t", as_of=datetime.now(UTC))
        ]
        with pytest.raises(RiskViolation):
            PositionSizeRule().check(ctx(buy("50"), portfolio=state))  # 8k + 5k > 10k

    def test_order_notional_bounds(self) -> None:
        big = Order(symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("1000"),
                    limit_price=Decimal("100"))
        with pytest.raises(RiskViolation, match="order_notional"):
            OrderNotionalRule().check(ctx(big))

    def test_spread_filter(self) -> None:
        with pytest.raises(RiskViolation, match="max_spread"):
            SpreadRule().check(ctx(buy(), spread="5.00"))  # 5% spread
        SpreadRule().check(ctx(buy(), spread="0.10"))

    def test_volume_filter(self) -> None:
        with pytest.raises(RiskViolation, match="min_volume"):
            VolumeRule().check(ctx(buy(), bars=_bars(volume=1_000)))
        with pytest.raises(RiskViolation, match="min_volume"):
            VolumeRule().check(ctx(buy(), bars=[]))

    def test_slippage_limit_price_band(self) -> None:
        with pytest.raises(RiskViolation, match="slippage"):
            SlippageProtectionRule().check(ctx(buy(limit="110.00")))  # 10% off quote
        SlippageProtectionRule().check(ctx(buy(limit="100.50")))

    def test_market_order_needs_tight_spread(self) -> None:
        order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                      quantity=Decimal("10"))
        with pytest.raises(RiskViolation):
            SlippageProtectionRule().check(ctx(order, spread="5.00"))

    def test_daily_loss_halt(self) -> None:
        state = portfolio_with(equity="96000")
        state.day_start_equity = Decimal("100000")  # -4% > 3% limit
        with pytest.raises(RiskViolation, match="max_daily_loss"):
            DailyLossRule().check(ctx(buy(), portfolio=state))

    def test_econ_blackout(self) -> None:
        event = EconomicEvent(name="FOMC Rate Decision", country="US",
                              scheduled_at=datetime.now(UTC) + timedelta(minutes=5),
                              importance="high", as_of=datetime.now(UTC), source="t")
        with pytest.raises(RiskViolation, match="news_blackout"):
            EconBlackoutRule().check(ctx(buy(), econ=[event]))
        # Low-importance events do not trigger the blackout.
        event_low = event.model_copy(update={"importance": "low"})
        EconBlackoutRule().check(ctx(buy(), econ=[event_low]))

    def test_cooldown(self) -> None:
        with pytest.raises(RiskViolation, match="cooldown"):
            CooldownRule().check(ctx(buy(), cooldown=120.0))


class TestCircuitBreaker:
    def test_opens_after_threshold(self) -> None:
        breaker = CircuitBreaker(error_threshold=3, window_seconds=60, cooldown_seconds=300)
        assert not breaker.is_open
        breaker.record_error()
        breaker.record_error()
        assert not breaker.is_open
        opened = breaker.record_error()
        assert opened and breaker.is_open
        assert "cooldown" in (breaker.reason or "")

    def test_force_open_and_close(self) -> None:
        breaker = CircuitBreaker(error_threshold=99, window_seconds=60, cooldown_seconds=1)
        breaker.force_open("manual halt")
        assert breaker.is_open and breaker.reason == "manual halt"
        breaker.force_close()
        assert not breaker.is_open

    def test_cooldowns(self) -> None:
        cooldowns = TradeCooldowns(per_symbol_seconds=300)
        assert cooldowns.remaining("AAPL") == 0
        cooldowns.record_trade("AAPL")
        assert cooldowns.remaining("aapl") > 290
        assert cooldowns.remaining("MSFT") == 0
