"""Risk rules, circuit breaker, and cooldowns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from poseidon.core.clock import FreshnessPolicy, MarketClock, MarketSession
from poseidon.core.config import RiskConfig
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.errors import CircuitBreakerOpen, RiskViolation
from poseidon.core.events import EventBus
from poseidon.core.models import (
    AccountSnapshot,
    Bar,
    EconomicEvent,
    OptionLeg,
    Order,
    Position,
)
from poseidon.data.router import DataRouter
from poseidon.portfolio.state import PortfolioState
from poseidon.risk.circuit import CircuitBreaker, TradeCooldowns
from poseidon.risk.engine import RiskEngine
from poseidon.risk.rules import (
    BuyingPowerRule,
    CooldownRule,
    DailyLossRule,
    EconBlackoutRule,
    FreshPortfolioRule,
    MarketOpenRule,
    OrderNotionalRule,
    PortfolioVaRRule,
    PositionSizeRule,
    RiskContext,
    SectorConcentrationRule,
    SlippageProtectionRule,
    SpreadRule,
    UniverseRule,
    VolumeRule,
)

from ..conftest import FakeProvider, make_quote


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

    def test_sell_to_open_goes_through_position_cap(self) -> None:
        # B1 regression: SELL_TO_OPEN increases (short) risk and must be capped
        # like a buy. The old `if not is_buy: return` guard exempted it.
        sto = Order(symbol="AAPL", side=OrderSide.SELL_TO_OPEN, order_type=OrderType.LIMIT,
                    quantity=Decimal("150"), limit_price=Decimal("100"))  # 15k > 10k cap
        with pytest.raises(RiskViolation, match="max_position_size"):
            PositionSizeRule().check(ctx(sto))

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


class TestUniverseRule:
    """A deterministic universe gate: opens outside the configured universe are
    denied by underlying; risk-reducing exits always pass so a position can never
    be trapped outside the universe."""

    def _buy(self, symbol: str = "AAPL") -> Order:
        return Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.LIMIT,
                     quantity=Decimal("10"), limit_price=Decimal("100.00"))

    def _sell(self, symbol: str = "AAPL") -> Order:
        return Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT,
                     quantity=Decimal("10"), limit_price=Decimal("100.00"))

    def _option_open(self, symbol: str) -> Order:
        return Order(symbol=symbol, asset_class=AssetClass.OPTION,
                     side=OrderSide.BUY_TO_OPEN, order_type=OrderType.LIMIT,
                     quantity=Decimal("1"), limit_price=Decimal("1.00"))

    def test_open_denied_on_exclude(self) -> None:
        cfg = RiskConfig(universe_exclude_symbols=["TSLA"])
        with pytest.raises(RiskViolation, match="universe"):
            UniverseRule().check(ctx(self._buy("TSLA"), config=cfg))
        UniverseRule().check(ctx(self._buy("AAPL"), config=cfg))  # not excluded, no allowlist

    def test_open_denied_off_allowlist(self) -> None:
        cfg = RiskConfig(universe_allow_symbols=["AAPL", "MSFT"])
        with pytest.raises(RiskViolation, match="universe"):
            UniverseRule().check(ctx(self._buy("TSLA"), config=cfg))
        UniverseRule().check(ctx(self._buy("AAPL"), config=cfg))  # on allowlist

    def test_exit_passes_even_when_excluded(self) -> None:
        # A denylisted (and off-allowlist) symbol can always be closed.
        cfg = RiskConfig(universe_exclude_symbols=["TSLA"], universe_allow_symbols=["AAPL"])
        UniverseRule().check(ctx(self._sell("TSLA"), config=cfg))

    def test_option_open_denied_by_underlying(self) -> None:
        # An excluded equity cannot be re-entered via its options: denial by
        # underlying, stripping the OCC tail.
        cfg = RiskConfig(universe_exclude_symbols=["AAPL"])
        with pytest.raises(RiskViolation, match="AAPL"):
            UniverseRule().check(ctx(self._option_open("AAPL240621C00190000"), config=cfg))
        # A different underlying's option is unaffected.
        UniverseRule().check(ctx(self._option_open("MSFT240621C00300000"), config=cfg))

    def test_case_insensitive(self) -> None:
        # Config normalizes to uppercase; a lowercase order symbol still matches.
        cfg = RiskConfig(universe_exclude_symbols=["tsla"])
        with pytest.raises(RiskViolation, match="universe"):
            UniverseRule().check(ctx(self._buy("tsla"), config=cfg))

    def test_empty_config_is_noop(self) -> None:
        cfg = RiskConfig()  # both lists empty: rule passes everything
        UniverseRule().check(ctx(self._buy("TSLA"), config=cfg))
        UniverseRule().check(ctx(self._sell("ANYTHING"), config=cfg))


class TestHaltFlattenWindow:
    """The circuit-breaker carve-out (§3.4). A tripped breaker rejects every
    normal order; ONLY kernel.halt()'s reduce-only flatten — carrying an
    identity-checked, unforgeable capability token issued by
    ``open_halt_flatten_window`` while the window is open and before its
    deadline — may pass the breaker, and only for a leg-free risk-reducing exit."""

    NOW = datetime.now(UTC)

    def _engine(self) -> RiskEngine:
        bus = EventBus()
        router = DataRouter([(FakeProvider(name="feed", price="100"), 10)], FreshnessPolicy())
        portfolio = PortfolioState()
        portfolio.account = AccountSnapshot(
            broker="paper", account_id="t", equity=Decimal("100000"),
            cash=Decimal("100000"), buying_power=Decimal("500000"), as_of=self.NOW,
        )
        # A long position so a reduce-only SELL passes ReduceOnlyRule.
        portfolio.positions = [Position(symbol="HELD", quantity=Decimal("1500"),
                                        avg_entry_price=Decimal("100"), as_of=self.NOW)]
        portfolio.synced_at = self.NOW
        config = RiskConfig(news_blackout_minutes_before_econ=0)
        return RiskEngine(config, portfolio, router, MarketClock(), bus)

    def _sell(self) -> Order:
        return Order(symbol="HELD", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                     quantity=Decimal("10"), limit_price=Decimal("100"))

    def _buy(self) -> Order:
        return Order(symbol="HELD", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                     quantity=Decimal("10"), limit_price=Decimal("100"))

    async def test_forged_token_rejected(self) -> None:
        # A fresh bare object() minted by anyone but the engine is NOT the live
        # capability: identity check fails, the tripped breaker still rejects.
        risk = self._engine()
        risk.circuit.force_open("halt")
        with pytest.raises(CircuitBreakerOpen):
            await risk.validate_order(self._sell(), halt_token=object())

    async def test_closed_window_rejects_real_token(self) -> None:
        # Even the genuinely-issued token is dead once the window is closed
        # (kernel.halt() closes it in a finally): the reference no longer
        # identity-matches the (now None) live token.
        risk = self._engine()
        risk.circuit.force_open("halt")
        token = risk.open_halt_flatten_window()
        risk.close_halt_flatten_window()
        with pytest.raises(CircuitBreakerOpen):
            await risk.validate_order(self._sell(), halt_token=token)

    async def test_deadline_expires_token(self) -> None:
        # A crashed flatten cannot leave the carve-out open forever: past the
        # monotonic deadline the token no longer permits, even while open.
        risk = self._engine()
        risk.circuit.force_open("halt")
        token = risk.open_halt_flatten_window(ttl_seconds=0.0)
        with pytest.raises(CircuitBreakerOpen):
            await risk.validate_order(self._sell(), halt_token=token)

    async def test_valid_token_never_admits_opening_order(self) -> None:
        # The permit is reduce-only: a live token on a BUY (opening exposure)
        # still hits the breaker. A stolen token cannot open new risk.
        risk = self._engine()
        risk.circuit.force_open("halt")
        token = risk.open_halt_flatten_window()
        with pytest.raises(CircuitBreakerOpen):
            await risk.validate_order(self._buy(), halt_token=token)

    async def test_valid_token_admits_reduce_only_exit(self) -> None:
        # The one sanctioned path: live token + open window + before deadline +
        # leg-free risk-reducing exit passes the breaker and runs the FULL rule
        # chain (returns the fresh quote, no CircuitBreakerOpen, no RiskViolation).
        risk = self._engine()
        risk.circuit.force_open("halt")
        token = risk.open_halt_flatten_window()
        with patch.object(MarketClock, "session", return_value=MarketSession.REGULAR):
            quote = await risk.validate_order(self._sell(), halt_token=token)
        assert quote is not None

    async def test_open_breaker_rejects_normal_order(self) -> None:
        # The single load-bearing invariant, pinned directly (elsewhere only
        # transitively covered): a tripped breaker rejects EVERY order that
        # presents no halt token — even a risk-reducing exit, the exact shape the
        # carve-out admits WITH a live token. Passing nothing (halt_token defaults
        # None) short-circuits the window entirely, so the breaker gate stands.
        risk = self._engine()
        risk.circuit.force_open("halt")
        with pytest.raises(CircuitBreakerOpen):
            await risk.validate_order(self._sell())

    async def test_valid_token_never_admits_multi_leg(self) -> None:
        # A live token on a risk-reducing exit that carries legs is STILL rejected:
        # halt_exit_permitted's ``and not order.legs`` clause bars any multi-leg
        # order from the carve-out. Identical to the admitted reduce-only exit above
        # but for the legs, so this isolates that clause — a stolen token can never
        # smuggle a multi-leg (e.g. spread-opening) order past a tripped breaker.
        risk = self._engine()
        risk.circuit.force_open("halt")
        token = risk.open_halt_flatten_window()
        multi_leg = Order(
            symbol="HELD", side=OrderSide.SELL, order_type=OrderType.LIMIT,
            quantity=Decimal("10"), limit_price=Decimal("100"),
            legs=[OptionLeg(contract_symbol="HELD_C1",
                            side=OrderSide.SELL_TO_CLOSE, quantity=1)],
        )
        with pytest.raises(CircuitBreakerOpen):
            await risk.validate_order(multi_leg, halt_token=token)


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


class _ClosedClock(MarketClock):
    """A clock that always reports the equity market as CLOSED (e.g. a weekend),
    so the crypto 24/7 exemption can be exercised deterministically."""

    def session(self, at: datetime | None = None) -> MarketSession:
        return MarketSession.CLOSED


def crypto_buy(qty: str = "0.05", limit: str = "30000.00", symbol: str = "BTC/USD") -> Order:
    return Order(symbol=symbol, asset_class=AssetClass.CRYPTO, side=OrderSide.BUY,
                 order_type=OrderType.LIMIT, quantity=Decimal(qty), limit_price=Decimal(limit))


class TestCryptoExemptions:
    """Crypto is exempt from EXACTLY two rules — the NYSE market-hours gate and
    the equity share-count volume floor — and from nothing else."""

    def test_market_open_rule_exempts_crypto_when_closed(self) -> None:
        closed = _ClosedClock()
        assert closed.session() is MarketSession.CLOSED
        # An equity order is blocked when the session is CLOSED...
        equity_ctx = ctx(buy())
        equity_ctx.clock = closed
        with pytest.raises(RiskViolation, match="market_session"):
            MarketOpenRule().check(equity_ctx)
        # ...but a crypto order trades 24/7 and passes despite the closed session.
        crypto_ctx = ctx(crypto_buy(), price="30000.00")
        crypto_ctx.clock = closed
        MarketOpenRule().check(crypto_ctx)

    def test_volume_rule_exempts_crypto(self) -> None:
        # BTC trades tens of thousands of COINS/day; a coin count near the 100k
        # SHARE floor is normal, so the share-count floor must not apply.
        low = _bars(volume=1_000)
        with pytest.raises(RiskViolation, match="min_volume"):
            VolumeRule().check(ctx(buy(), bars=low))  # equity: floor fires
        VolumeRule().check(ctx(crypto_buy(), price="30000.00", bars=low))  # crypto: exempt
        # Crypto is exempt even with no bar history at all.
        VolumeRule().check(ctx(crypto_buy(), price="30000.00", bars=[]))

    def test_other_rules_still_fire_for_crypto(self) -> None:
        # Notional cap: a whole BTC at 30k is > the 25k max_order_notional and is
        # correctly rejected (size fractionally), same as any oversized equity.
        big = crypto_buy(qty="1", limit="30000.00")
        with pytest.raises(RiskViolation, match="order_notional"):
            OrderNotionalRule().check(ctx(big, price="30000.00"))
        # Spread filter is asset-class-neutral: a wide crypto book is rejected.
        with pytest.raises(RiskViolation, match="max_spread"):
            SpreadRule().check(ctx(crypto_buy(), price="30000.00", spread="3000.00"))


class TestSleeveOverride:
    """A dedicated sleeve substitutes ONLY the per-position cap."""

    def _order(self, strategy: str, qty: str) -> Order:
        return Order(symbol="TQQQ", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                     quantity=Decimal(qty), limit_price=Decimal("100.00"),
                     strategy=strategy)

    def test_sleeve_raises_cap_for_its_strategy_only(self) -> None:
        # 100k equity; default cap 10% blocks a 40k order, a 50% sleeve allows it.
        # The sleeve only applies to a symbol the strategy actually signalled
        # this cycle (trusted attribution), never the AI-supplied strategy tag.
        context = ctx(self._order("algo:rot", "400"))
        context.sleeve_caps = {"algo:rot": 0.5}
        context.sleeve_attribution = {"algo:rot": {"TQQQ"}}
        PositionSizeRule().check(context)
        # Same order tag but the symbol was NOT attributed to the sleeve: the
        # spoof-proof path falls back to the default cap and blocks it.
        spoofed = ctx(self._order("algo:rot", "400"))
        spoofed.sleeve_caps = {"algo:rot": 0.5}
        spoofed.sleeve_attribution = {"algo:rot": set()}
        with pytest.raises(RiskViolation, match="max_position_pct"):
            PositionSizeRule().check(spoofed)
        blocked = ctx(self._order("momentum", "400"))
        blocked.sleeve_caps = {"algo:rot": 0.5}
        with pytest.raises(RiskViolation, match="max_position_pct"):
            PositionSizeRule().check(blocked)

    def test_sleeve_is_still_a_ceiling(self) -> None:
        context = ctx(self._order("algo:rot", "600"))  # 60k > 50% sleeve
        context.sleeve_caps = {"algo:rot": 0.5}
        context.sleeve_attribution = {"algo:rot": {"TQQQ"}}
        with pytest.raises(RiskViolation, match="sleeve"):
            PositionSizeRule().check(context)

    def test_other_rules_unaffected_by_sleeve(self) -> None:
        # Gross-exposure cap still applies to sleeve orders (1.0x equity).
        from poseidon.risk.rules import PortfolioExposureRule

        context = ctx(self._order("algo:rot", "1100"))  # 110k > 100k gross cap
        context.sleeve_caps = {"algo:rot": 1.0}
        with pytest.raises(RiskViolation, match="gross exposure"):
            PortfolioExposureRule().check(context)
