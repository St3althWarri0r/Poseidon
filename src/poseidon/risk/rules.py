"""Pre-trade risk rules.

Each rule is a small, independently-testable class with a single ``check``
method that raises :class:`RiskViolation` on breach. The engine runs every
rule for every order — there is no fast path that skips checks.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from ..core.clock import MarketClock
from ..core.config import RiskConfig
from ..core.enums import AssetClass, MarketSession
from ..core.errors import RiskViolation
from ..core.models import Bar, EconomicEvent, Order, Quote
from ..portfolio.state import PortfolioState

MAX_STATE_AGE_SECONDS = 120.0


@dataclass
class RiskContext:
    order: Order
    quote: Quote  # live, freshness-checked quote for the order's symbol
    portfolio: PortfolioState
    config: RiskConfig
    clock: MarketClock
    recent_bars: list[Bar] = field(default_factory=list)  # daily bars for volume/volatility
    upcoming_econ: list[EconomicEvent] = field(default_factory=list)
    orders_today: int = 0
    cooldown_remaining: float = 0.0
    # Sector taxonomy (None = unknown/unavailable; ETFs have no sector).
    order_sector: str | None = None
    position_sectors: dict[str, str] = field(default_factory=dict)
    # Dedicated sleeves: strategy -> per-position equity fraction override.
    sleeve_caps: dict[str, float] = field(default_factory=dict)

    @property
    def reference_price(self) -> Decimal:
        price = self.quote.mid or self.quote.last
        if price is None or price <= 0:
            raise RiskViolation("reference_price", f"no usable live price for {self.order.symbol}")
        return price

    @property
    def notional(self) -> Decimal:
        multiplier = Decimal(100) if self.order.asset_class is AssetClass.OPTION else Decimal(1)
        notional = self.order.estimated_notional(self.reference_price)
        if notional is None:  # unreachable: reference_price already raised if unusable
            raise RiskViolation("reference_price", f"no usable notional for {self.order.symbol}")
        return notional * multiplier


class RiskRule(abc.ABC):
    name: str = ""

    @abc.abstractmethod
    def check(self, ctx: RiskContext) -> None: ...


class FreshPortfolioRule(RiskRule):
    """Refuse to trade against portfolio state that hasn't synced recently."""

    name = "fresh_portfolio_state"

    def check(self, ctx: RiskContext) -> None:
        age = ctx.portfolio.age_seconds
        if age is None:
            raise RiskViolation(self.name, "portfolio has never synced")
        if age > MAX_STATE_AGE_SECONDS:
            raise RiskViolation(self.name, f"portfolio state is {age:.0f}s old (max {MAX_STATE_AGE_SECONDS:.0f}s)")


class MarketOpenRule(RiskRule):
    """Regular-hours only, unless the order explicitly requests extended hours.
    Also covers holidays and (via the clock's fail-safe) unknown calendar
    years and exchange-wide halts surfaced as CLOSED."""

    name = "market_session"

    def check(self, ctx: RiskContext) -> None:
        session = ctx.clock.session()
        if session is MarketSession.REGULAR:
            return
        if ctx.order.extended_hours and session in (MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS):
            return
        raise RiskViolation(self.name, f"market session is {session}, order not eligible")


class BuyingPowerRule(RiskRule):
    name = "buying_power"

    def check(self, ctx: RiskContext) -> None:
        if not ctx.order.side.is_buy:
            return
        account = ctx.portfolio.account
        if account is None:
            raise RiskViolation(self.name, "no account snapshot")
        bp = account.options_buying_power if (
            ctx.order.asset_class is AssetClass.OPTION and account.options_buying_power is not None
        ) else account.buying_power
        if ctx.notional > bp:
            raise RiskViolation(self.name, f"notional {ctx.notional:.2f} exceeds buying power {bp:.2f}")


class PositionSizeRule(RiskRule):
    """Per-position cap. An order from a strategy with a dedicated sleeve
    uses the sleeve as its cap instead — a concentrated rotation model can
    run at full weight inside its allocation while everything else keeps
    the tighter institutional limit. Sleeves only substitute this one
    rule: gross exposure, leverage, loss halts, liquidity filters, and
    every other rule still apply unchanged."""

    name = "max_position_size"

    def check(self, ctx: RiskContext) -> None:
        if not ctx.order.side.is_buy:
            return
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        existing = Decimal(0)
        position = ctx.portfolio.position_for(ctx.order.symbol)
        if position is not None and position.market_value is not None:
            existing = abs(position.market_value)
        sleeve = ctx.sleeve_caps.get(ctx.order.strategy)
        cap_pct = sleeve if sleeve is not None else ctx.config.max_position_pct
        limit = equity * Decimal(str(cap_pct))
        if existing + ctx.notional > limit:
            source = "sleeve" if sleeve is not None else "max_position_pct"
            raise RiskViolation(
                self.name,
                f"position would be {(existing + ctx.notional):.2f}, limit {limit:.2f} "
                f"({cap_pct:.0%} of equity, {source})",
            )


class PortfolioExposureRule(RiskRule):
    name = "max_portfolio_exposure"

    def check(self, ctx: RiskContext) -> None:
        if not ctx.order.side.is_buy:
            return
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        gross = ctx.portfolio.gross_exposure() + ctx.notional
        limit = equity * Decimal(str(ctx.config.max_portfolio_exposure_pct))
        if gross > limit:
            raise RiskViolation(self.name, f"gross exposure would be {gross:.2f}, limit {limit:.2f}")


class LeverageRule(RiskRule):
    name = "max_leverage"

    def check(self, ctx: RiskContext) -> None:
        if not ctx.order.side.is_buy:
            return
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        gross = ctx.portfolio.gross_exposure() + ctx.notional
        leverage = gross / equity
        if float(leverage) > ctx.config.max_leverage:
            raise RiskViolation(self.name, f"leverage would be {float(leverage):.2f}x, max {ctx.config.max_leverage}x")


class OptionsExposureRule(RiskRule):
    name = "max_options_exposure"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.asset_class is not AssetClass.OPTION or not ctx.order.side.is_buy:
            return
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        exposure = ctx.portfolio.options_exposure() + ctx.notional
        limit = equity * Decimal(str(ctx.config.max_options_exposure_pct))
        if exposure > limit:
            raise RiskViolation(self.name, f"options exposure would be {exposure:.2f}, limit {limit:.2f}")


class SectorConcentrationRule(RiskRule):
    """Cap total exposure to any one sector. Enforced whenever a sector
    classification is available for the order's symbol (a SECTOR-capable
    provider such as Finnhub); when the symbol cannot be classified (ETFs,
    provider gap) the rule passes and the AI enforces the cap qualitatively
    — a taxonomy gap must not halt all trading."""

    name = "max_sector_concentration"

    def check(self, ctx: RiskContext) -> None:
        if not ctx.order.side.is_buy or ctx.order.asset_class is not AssetClass.EQUITY:
            return
        sector = ctx.order_sector
        if not sector:
            return  # unknown sector: qualitative enforcement (documented)
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        exposure = ctx.notional
        order_symbol = ctx.order.symbol.upper()
        for position in ctx.portfolio.positions:
            symbol = position.symbol.upper()
            # The order's own symbol always counts toward its sector.
            if symbol != order_symbol and ctx.position_sectors.get(symbol) != sector:
                continue
            value = position.market_value
            if value is None:
                value = position.quantity * position.avg_entry_price
            exposure += abs(value)
        limit = equity * Decimal(str(ctx.config.max_sector_concentration_pct))
        if exposure > limit:
            raise RiskViolation(
                self.name,
                f"'{sector}' exposure would be {exposure:.2f}, limit {limit:.2f} "
                f"({ctx.config.max_sector_concentration_pct:.0%} of equity)",
            )


class PortfolioVaRRule(RiskRule):
    """Optional halt on new risk when the book's 1-day historical VaR(95)
    exceeds the configured fraction of equity. Metrics are computed on a
    schedule from live bar history; enabling this rule makes FRESH metrics a
    requirement for opening new risk (no metrics, no new positions)."""

    name = "max_portfolio_var"
    max_metrics_age_seconds = 3600.0

    def check(self, ctx: RiskContext) -> None:
        cap = ctx.config.max_portfolio_var_pct
        if cap <= 0 or not ctx.order.side.is_buy:
            return
        metrics = ctx.portfolio.risk_metrics
        age = ctx.portfolio.risk_metrics_age_seconds()
        if metrics is None or age is None or age > self.max_metrics_age_seconds:
            raise RiskViolation(
                self.name,
                "portfolio VaR limit is enabled but fresh risk metrics are unavailable "
                f"(age: {'never computed' if age is None else f'{age:.0f}s'}) — "
                "no new risk without a current VaR estimate",
            )
        var_pct = metrics.get("var_95_pct")
        if not isinstance(var_pct, int | float):
            raise RiskViolation(self.name, "risk metrics present but VaR missing")
        if var_pct >= cap:
            raise RiskViolation(
                self.name,
                f"portfolio 1-day VaR(95) {var_pct:.2%} >= limit {cap:.2%} — "
                "reduce risk before adding positions",
            )


class OrderNotionalRule(RiskRule):
    name = "order_notional_bounds"

    def check(self, ctx: RiskContext) -> None:
        if ctx.notional > ctx.config.max_order_notional:
            raise RiskViolation(self.name, f"notional {ctx.notional:.2f} exceeds max {ctx.config.max_order_notional}")
        if ctx.notional < ctx.config.min_order_notional:
            raise RiskViolation(self.name, f"notional {ctx.notional:.2f} below min {ctx.config.min_order_notional}")


class SpreadRule(RiskRule):
    """Liquidity filter: refuse symbols with wide bid/ask spreads."""

    name = "max_spread"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return  # entry filter: never trap an exit in a widened book
        spread = ctx.quote.spread_pct
        if spread is None:
            # One-sided book: fine for research quotes, not for orders.
            raise RiskViolation(self.name, f"no two-sided quote for {ctx.order.symbol}")
        if float(spread) > ctx.config.max_spread_pct:
            raise RiskViolation(
                self.name, f"spread {float(spread):.2%} exceeds limit {ctx.config.max_spread_pct:.2%}"
            )


class VolumeRule(RiskRule):
    """Liquidity filter: require adequate average daily volume."""

    name = "min_volume"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return  # entry filter only
        if ctx.order.asset_class is AssetClass.OPTION:
            return  # option liquidity is screened via the chain's OI upstream
        if not ctx.recent_bars:
            raise RiskViolation(self.name, "no volume history available")
        window = ctx.recent_bars[-20:]
        avg = sum(b.volume for b in window) / len(window)
        if avg < ctx.config.min_avg_volume:
            raise RiskViolation(self.name, f"20d avg volume {avg:,.0f} below minimum {ctx.config.min_avg_volume:,}")


class SlippageProtectionRule(RiskRule):
    """Market orders are only allowed with a tight spread; limit prices must
    be within the slippage band of the live quote (fat-finger guard)."""

    name = "slippage_protection"

    def check(self, ctx: RiskContext) -> None:
        band = Decimal(str(ctx.config.slippage_limit_pct))
        reference = ctx.reference_price
        if ctx.order.limit_price is not None:
            deviation = abs(ctx.order.limit_price - reference) / reference
            if deviation > band:
                raise RiskViolation(
                    self.name,
                    f"limit {ctx.order.limit_price} is {float(deviation):.2%} from live price "
                    f"{reference:.2f} (band {ctx.config.slippage_limit_pct:.2%})",
                )
        elif ctx.order.order_type.value == "market":
            spread = ctx.quote.spread_pct
            if spread is None or float(spread) > ctx.config.slippage_limit_pct:
                raise RiskViolation(self.name, "market order refused: spread too wide or book one-sided")


class DailyLossRule(RiskRule):
    """Halts NEW risk for the day. Risk-reducing orders (exits, hedges
    closing) are exempt — a loss halt must never trap the operator in a
    losing position."""

    name = "max_daily_loss"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return
        loss = ctx.portfolio.day_loss_pct()
        if loss >= ctx.config.max_daily_loss_pct:
            raise RiskViolation(self.name, f"daily loss {loss:.2%} >= limit {ctx.config.max_daily_loss_pct:.2%} — trading halted for the day")


class WeeklyLossRule(RiskRule):
    name = "max_weekly_loss"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return
        loss = ctx.portfolio.week_loss_pct()
        if loss >= ctx.config.max_weekly_loss_pct:
            raise RiskViolation(self.name, f"weekly loss {loss:.2%} >= limit {ctx.config.max_weekly_loss_pct:.2%} — trading halted for the week")


class DrawdownRule(RiskRule):
    name = "max_drawdown"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return
        dd = ctx.portfolio.drawdown_pct()
        if dd >= ctx.config.max_drawdown_pct:
            raise RiskViolation(self.name, f"drawdown {dd:.2%} >= limit {ctx.config.max_drawdown_pct:.2%} — trading halted")


class VolatilityHaltRule(RiskRule):
    """Halt new entries when the symbol itself has moved violently today —
    a per-name analogue of exchange circuit breakers."""

    name = "volatility_halt"

    def check(self, ctx: RiskContext) -> None:
        if not ctx.order.side.is_buy or not ctx.recent_bars:
            return
        last = ctx.recent_bars[-1]
        if last.open <= 0:
            return
        move = abs(last.close - last.open) / last.open
        if float(move) >= ctx.config.volatility_halt_daily_move_pct:
            raise RiskViolation(
                self.name,
                f"{ctx.order.symbol} moved {float(move):.2%} today, above the "
                f"{ctx.config.volatility_halt_daily_move_pct:.2%} volatility filter",
            )


class EconBlackoutRule(RiskRule):
    """No new entries in the minutes before high-importance economic releases."""

    name = "news_blackout"

    def check(self, ctx: RiskContext) -> None:
        if not ctx.order.side.is_buy or ctx.config.news_blackout_minutes_before_econ <= 0:
            return
        now = datetime.now(UTC)
        window = ctx.config.news_blackout_minutes_before_econ * 60
        for event in ctx.upcoming_econ:
            importance = (event.importance or "").lower()
            if importance not in ("high", "3"):
                continue
            delta = (event.scheduled_at - now).total_seconds()
            if 0 <= delta <= window:
                raise RiskViolation(
                    self.name,
                    f"blackout: '{event.name}' ({event.country}) in {delta/60:.0f} min",
                )


class OrdersPerDayRule(RiskRule):
    name = "max_orders_per_day"

    def check(self, ctx: RiskContext) -> None:
        if ctx.orders_today >= ctx.config.max_orders_per_day:
            raise RiskViolation(self.name, f"{ctx.orders_today} orders today, limit {ctx.config.max_orders_per_day}")


class CooldownRule(RiskRule):
    name = "trade_cooldown"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return  # cooldowns prevent re-entry churn, never delay exits
        if ctx.cooldown_remaining > 0:
            raise RiskViolation(
                self.name, f"{ctx.order.symbol} in cooldown for {ctx.cooldown_remaining:.0f}s more"
            )


ALL_RULES: list[RiskRule] = [
    FreshPortfolioRule(),
    MarketOpenRule(),
    DailyLossRule(),
    WeeklyLossRule(),
    DrawdownRule(),
    OrdersPerDayRule(),
    CooldownRule(),
    OrderNotionalRule(),
    BuyingPowerRule(),
    PositionSizeRule(),
    PortfolioExposureRule(),
    LeverageRule(),
    OptionsExposureRule(),
    SectorConcentrationRule(),
    PortfolioVaRRule(),
    SpreadRule(),
    VolumeRule(),
    SlippageProtectionRule(),
    VolatilityHaltRule(),
    EconBlackoutRule(),
]
