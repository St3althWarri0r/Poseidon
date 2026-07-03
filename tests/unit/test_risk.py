"""Risk rules, circuit breaker, and cooldowns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.core.clock import MarketClock
from poseidon.core.config import RiskConfig
from poseidon.core.enums import OrderSide, OrderType
from poseidon.core.errors import RiskViolation
from poseidon.core.models import AccountSnapshot, Bar, EconomicEvent, Order, Position
from poseidon.portfolio.state import PortfolioState
from poseidon.risk.circuit import CircuitBreaker, TradeCooldowns
from poseidon.risk.rules import (
    BuyingPowerRule,
    CooldownRule,
    DailyLossRule,
    EconBlackoutRule,
    FreshPortfolioRule,
    OrderNotionalRule,
    PortfolioVaRRule,
    PositionSizeRule,
    RiskContext,
    SectorConcentrationRule,
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
        cooldown: float = 0.0, config: RiskConfig | None = None,
        order_sector: str | None = None,
        position_sectors: dict[str, str] | None = None) -> RiskContext:
    return RiskContext(
        order=order,
        quote=make_quote(order.symbol, price, spread=spread),
        portfolio=portfolio or portfolio_with(),
        config=config or RiskConfig(),
        clock=MarketClock(),
        recent_bars=bars if bars is not None else _bars(),
        upcoming_econ=econ or [],
        orders_today=orders_today,
        cooldown_remaining=cooldown,
        order_sector=order_sector,
        position_sectors=position_sectors or {},
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


class TestRiskReducingExemptions:
    """Halts and entry filters must never trap the operator in a position."""

    def _sell(self, qty: str = "10") -> Order:
        return Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                     quantity=Decimal(qty), limit_price=Decimal("100.00"))

    def test_daily_loss_halt_allows_exits(self) -> None:
        state = portfolio_with(equity="90000")
        state.day_start_equity = Decimal("100000")  # -10%: buys blocked
        with pytest.raises(RiskViolation):
            DailyLossRule().check(ctx(buy(), portfolio=state))
        DailyLossRule().check(ctx(self._sell(), portfolio=state))  # sell passes

    def test_spread_filter_allows_exits(self) -> None:
        SpreadRule().check(ctx(self._sell(), spread="5.00"))  # wide book, exit OK

    def test_volume_filter_allows_exits(self) -> None:
        VolumeRule().check(ctx(self._sell(), bars=[]))

    def test_cooldown_allows_exits(self) -> None:
        CooldownRule().check(ctx(self._sell(), cooldown=120.0))

    def test_sell_to_open_is_not_exempt(self) -> None:
        assert not OrderSide.SELL_TO_OPEN.is_risk_reducing  # opening short risk
        assert OrderSide.SELL.is_risk_reducing
        assert OrderSide.BUY_TO_CLOSE.is_risk_reducing


class TestSectorConcentration:
    def _portfolio_with_tech(self) -> PortfolioState:
        state = portfolio_with()  # 100k equity; 30% cap -> 30k sector budget
        state.positions = [
            Position(symbol="MSFT", quantity=Decimal("100"), avg_entry_price=Decimal("250"),
                     market_value=Decimal("25000"), broker="t", as_of=datetime.now(UTC)),
            Position(symbol="XOM", quantity=Decimal("100"), avg_entry_price=Decimal("100"),
                     market_value=Decimal("10000"), broker="t", as_of=datetime.now(UTC)),
        ]
        return state

    def test_blocks_over_concentration(self) -> None:
        # 25k existing Technology + 10k order > 30k cap.
        with pytest.raises(RiskViolation, match="max_sector_concentration"):
            SectorConcentrationRule().check(ctx(
                buy("100"), portfolio=self._portfolio_with_tech(),
                order_sector="Technology",
                position_sectors={"MSFT": "Technology", "XOM": "Energy"},
            ))

    def test_allows_within_cap_and_other_sectors(self) -> None:
        SectorConcentrationRule().check(ctx(
            buy("40"), portfolio=self._portfolio_with_tech(),  # 25k + 4k <= 30k
            order_sector="Technology",
            position_sectors={"MSFT": "Technology", "XOM": "Energy"},
        ))
        SectorConcentrationRule().check(ctx(
            buy("100"), portfolio=self._portfolio_with_tech(),  # Energy 10k + 10k
            order_sector="Energy",
            position_sectors={"MSFT": "Technology", "XOM": "Energy"},
        ))

    def test_unknown_sector_passes(self) -> None:
        # No taxonomy available: the rule defers to qualitative enforcement.
        SectorConcentrationRule().check(ctx(
            buy("100"), portfolio=self._portfolio_with_tech(), order_sector=None,
        ))

    def test_own_position_counts_even_unclassified(self) -> None:
        state = portfolio_with()
        state.positions = [
            Position(symbol="AAPL", quantity=Decimal("250"), avg_entry_price=Decimal("100"),
                     market_value=Decimal("25000"), broker="t", as_of=datetime.now(UTC)),
        ]
        with pytest.raises(RiskViolation):
            SectorConcentrationRule().check(ctx(
                buy("100"), portfolio=state, order_sector="Technology",
                position_sectors={},  # AAPL missing from map; still its own sector
            ))

    def test_sells_exempt(self) -> None:
        order = Order(symbol="MSFT", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                      quantity=Decimal("100"), limit_price=Decimal("100.00"))
        SectorConcentrationRule().check(ctx(
            order, portfolio=self._portfolio_with_tech(), order_sector="Technology",
            position_sectors={"MSFT": "Technology"},
        ))


class TestPortfolioVaRRule:
    def _config(self, cap: float) -> RiskConfig:
        return RiskConfig(max_portfolio_var_pct=cap)

    def test_disabled_by_default(self) -> None:
        PortfolioVaRRule().check(ctx(buy()))

    def test_requires_fresh_metrics_when_enabled(self) -> None:
        state = portfolio_with()
        with pytest.raises(RiskViolation, match="max_portfolio_var"):
            PortfolioVaRRule().check(ctx(buy(), portfolio=state, config=self._config(0.05)))

    def test_blocks_over_var(self) -> None:
        state = portfolio_with()
        state.risk_metrics = {"var_95_pct": 0.08}
        state.risk_metrics_at = datetime.now(UTC)
        with pytest.raises(RiskViolation, match="VaR"):
            PortfolioVaRRule().check(ctx(buy(), portfolio=state, config=self._config(0.05)))

    def test_allows_under_var_and_exits_always(self) -> None:
        state = portfolio_with()
        state.risk_metrics = {"var_95_pct": 0.02}
        state.risk_metrics_at = datetime.now(UTC)
        PortfolioVaRRule().check(ctx(buy(), portfolio=state, config=self._config(0.05)))
        sell = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                     quantity=Decimal("10"), limit_price=Decimal("100.00"))
        state.risk_metrics = {"var_95_pct": 0.50}
        PortfolioVaRRule().check(ctx(sell, portfolio=state, config=self._config(0.05)))

    def test_stale_metrics_block(self) -> None:
        state = portfolio_with()
        state.risk_metrics = {"var_95_pct": 0.01}
        state.risk_metrics_at = datetime.now(UTC) - timedelta(hours=3)
        with pytest.raises(RiskViolation, match="fresh"):
            PortfolioVaRRule().check(ctx(buy(), portfolio=state, config=self._config(0.05)))


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
