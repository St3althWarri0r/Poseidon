"""Pre-trade risk rules.

Each rule is a small, independently-testable class with a single ``check``
method that raises :class:`RiskViolation` on breach. The engine runs every
rule for every order — there is no fast path that skips checks.
"""

from __future__ import annotations

import abc
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from ..core.clock import MarketClock
from ..core.config import RiskConfig
from ..core.enums import AssetClass, MarketSession, OrderSide
from ..core.errors import RiskViolation
from ..core.models import Bar, EconomicEvent, Order, Quote
from ..portfolio.state import PortfolioState

MAX_STATE_AGE_SECONDS = 120.0

_OCC_STRIKE_RE = re.compile(r"[CP](\d{8})$")

# OCC contract tail: 6-digit expiry date + C/P + 8-digit strike. Stripping it
# from an option symbol yields the underlying (e.g. AAPL240621C00190000 -> AAPL).
_OCC_TAIL_RE = re.compile(r"\d{6}[CP]\d{8}$")


def _underlying(order: Order) -> str:
    """The underlying symbol an order trades, uppercased. Multi-leg option
    parents already carry the underlying in ``order.symbol`` (see
    ``reduce_only_breach`` below); a single-leg OPTION order carries an OCC
    contract symbol whose trailing tail is stripped; everything else is the
    order symbol itself."""
    symbol = order.symbol.strip().upper()
    if order.legs:
        return symbol
    if order.asset_class is AssetClass.OPTION:
        return _OCC_TAIL_RE.sub("", symbol)
    return symbol


def _option_strike(occ_symbol: str) -> Decimal | None:
    """Strike from an OCC option symbol (e.g. AAPL240621C00190000 -> 190).
    The trailing 8 digits encode strike*1000. Returns None if the symbol is
    not OCC-formatted."""
    match = _OCC_STRIKE_RE.search(occ_symbol.strip().upper())
    if match is None:
        return None
    return Decimal(match.group(1)) / Decimal(1000)


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
    # In-flight exposure reserved by the engine for orders submitted but not
    # yet visible in a portfolio sync, so several orders validated against the
    # same snapshot cannot stack past the exposure/leverage caps.
    pending_gross: Decimal = Decimal(0)  # in-flight risk-increasing notional
    pending_options: Decimal = Decimal(0)
    pending_by_symbol: dict[str, Decimal] = field(default_factory=dict)
    # Sector taxonomy (None = unknown/unavailable; ETFs have no sector).
    order_sector: str | None = None
    position_sectors: dict[str, str] = field(default_factory=dict)
    # Dedicated sleeves: strategy -> per-position equity fraction override.
    sleeve_caps: dict[str, float] = field(default_factory=dict)
    # Trusted attribution built by the engine from this cycle's real signals:
    # strategy name -> the symbols it actually signalled. A sleeve applies to
    # an order only if the order's symbol is in its strategy's set, so the AI
    # cannot claim a sleeved strategy's larger cap for an arbitrary symbol.
    sleeve_attribution: dict[str, set[str]] = field(default_factory=dict)

    @property
    def reference_price(self) -> Decimal:
        price = self.quote.mid or self.quote.last
        if price is None or price <= 0:
            raise RiskViolation("reference_price", f"no usable live price for {self.order.symbol}")
        return price

    @property
    def notional(self) -> Decimal:
        # A short option open must be sized by its assignment/margin basis
        # (strike x 100 x qty), NOT the premium received — premium sizing
        # understates capital at risk by orders of magnitude and lets a naked
        # short slip past buying-power, position-size, exposure and leverage
        # caps. Multi-leg packages (OptionLeg is options-only) are sized at the
        # sum of their SELL_TO_OPEN legs' strike bases regardless of the
        # order-level side or asset_class: nothing here verifies that a long
        # leg covers a short one, so short legs are deliberately over-sized
        # (fail safe) rather than trusted as defined risk. Long-only packages
        # keep premium (net debit) sizing.
        if self.order.legs:
            short_basis = Decimal(0)
            for leg in self.order.legs:
                if leg.side is not OrderSide.SELL_TO_OPEN:
                    continue
                strike = _option_strike(leg.contract_symbol)
                if strike is None:
                    raise RiskViolation(
                        "notional",
                        f"cannot determine the strike for short leg {leg.contract_symbol}; "
                        "refusing to size a short option by premium received",
                    )
                short_basis += strike * Decimal(100) * Decimal(leg.quantity) * abs(self.order.quantity)
            if short_basis > 0:
                return short_basis
        elif (self.order.asset_class is AssetClass.OPTION
                and self.order.side is OrderSide.SELL_TO_OPEN):
            strike = _option_strike(self.order.symbol)
            if strike is None:
                raise RiskViolation(
                    "notional",
                    f"cannot determine the strike for short option {self.order.symbol}; "
                    "refusing to size an uncovered short by premium received",
                )
            return strike * Decimal(100) * abs(self.order.quantity)
        multiplier = Decimal(100) if (
            self.order.asset_class is AssetClass.OPTION or self.order.legs
        ) else Decimal(1)
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
    years and exchange-wide halts surfaced as CLOSED.
    Crypto orders are exempt: crypto markets trade continuously."""

    name = "market_session"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.asset_class is AssetClass.CRYPTO:
            return  # crypto trades 24/7 — the NYSE session gate does not apply
        session = ctx.clock.session()
        if session is MarketSession.REGULAR:
            return
        if ctx.order.extended_hours and session in (MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS):
            return
        raise RiskViolation(self.name, f"market session is {session}, order not eligible")


class UniverseRule(RiskRule):
    """Deterministic trading-universe gate. Opens outside the configured
    universe are denied by underlying (an excluded equity cannot be re-entered
    via its options); risk-reducing exits ALWAYS pass so a position can never be
    trapped outside the universe. Exclude wins over allow. Both lists empty ships
    as a no-op (conservative default = no behavior change). Purely sync and
    config-driven: no data fetch, no LLM."""

    name = "universe"

    def check(self, ctx: RiskContext) -> None:
        # Risk-reducing exits always pass — but a multi-leg order whose parent
        # side reads reducing while a leg OPENS is still an open and is gated.
        if ctx.order.side.is_risk_reducing and not any(
            leg.side in (OrderSide.BUY_TO_OPEN, OrderSide.SELL_TO_OPEN)
            for leg in ctx.order.legs
        ):
            return
        symbol = _underlying(ctx.order)
        if symbol in set(ctx.config.universe_exclude_symbols):
            raise RiskViolation(self.name, f"{symbol} is on universe_exclude_symbols")
        allow = ctx.config.universe_allow_symbols
        if allow and symbol not in set(allow):
            raise RiskViolation(self.name, f"{symbol} is not on universe_allow_symbols")


class BuyingPowerRule(RiskRule):
    name = "buying_power"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return  # only exits are exempt; risk-INCREASING sides (incl. sell_to_open) pass
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
        if ctx.order.side.is_risk_reducing:
            return  # only exits are exempt; risk-INCREASING sides (incl. sell_to_open) pass
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        existing = Decimal(0)
        position = ctx.portfolio.position_for(ctx.order.symbol)
        if position is not None and position.market_value is not None:
            existing = abs(position.market_value)
        existing += ctx.pending_by_symbol.get(ctx.order.symbol.upper(), Decimal(0))
        # A sleeve applies only if this order's symbol was actually signalled
        # by the sleeved strategy this cycle — the order's self-declared
        # `strategy` string (AI-controlled) is not trusted on its own.
        strategy = ctx.order.strategy
        sleeve = ctx.sleeve_caps.get(strategy)
        if sleeve is not None and ctx.order.symbol.upper() not in ctx.sleeve_attribution.get(strategy, set()):
            sleeve = None
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
        if ctx.order.side.is_risk_reducing:
            return  # only exits are exempt; risk-INCREASING sides (incl. sell_to_open) pass
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        gross = ctx.portfolio.gross_exposure() + ctx.pending_gross + ctx.notional
        limit = equity * Decimal(str(ctx.config.max_portfolio_exposure_pct))
        if gross > limit:
            raise RiskViolation(self.name, f"gross exposure would be {gross:.2f}, limit {limit:.2f}")


class LeverageRule(RiskRule):
    name = "max_leverage"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return  # only exits are exempt; risk-INCREASING sides (incl. sell_to_open) pass
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        gross = ctx.portfolio.gross_exposure() + ctx.pending_gross + ctx.notional
        leverage = gross / equity
        if float(leverage) > ctx.config.max_leverage:
            raise RiskViolation(self.name, f"leverage would be {float(leverage):.2f}x, max {ctx.config.max_leverage}x")


class OptionsExposureRule(RiskRule):
    name = "max_options_exposure"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.asset_class is not AssetClass.OPTION or ctx.order.side.is_risk_reducing:
            return
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        exposure = ctx.portfolio.options_exposure() + ctx.pending_options + ctx.notional
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
        if ctx.order.side.is_risk_reducing or ctx.order.asset_class is not AssetClass.EQUITY:
            return
        sector = ctx.order_sector
        if not sector:
            return  # unknown sector: qualitative enforcement (documented)
        equity = ctx.portfolio.equity
        if equity is None or equity <= 0:
            raise RiskViolation(self.name, "no equity snapshot")
        order_symbol = ctx.order.symbol.upper()
        # Own-symbol in-flight notional is same-sector by definition.
        exposure = ctx.notional + ctx.pending_by_symbol.get(order_symbol, Decimal(0))
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
        if cap <= 0 or ctx.order.side.is_risk_reducing:
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
        if ctx.order.side.is_risk_reducing:
            # The min bound is an anti-churn entry filter. It must not apply to
            # exits: unlike an over-max exit (which can be split), an under-min
            # exit cannot be restructured to pass, so a sub-min position could
            # never be closed — guardian stops included.
            return
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
    """Liquidity filter: require adequate average daily volume.
    Crypto is exempt: ``min_avg_volume`` is a share-count floor and crypto
    ``Bar.volume`` is a coin count — its liquidity is gated by spread instead."""

    name = "min_volume"

    def check(self, ctx: RiskContext) -> None:
        if ctx.order.side.is_risk_reducing:
            return  # entry filter only
        if ctx.order.asset_class is AssetClass.OPTION:
            return  # option liquidity is screened via the chain's OI upstream
        if ctx.order.asset_class is AssetClass.CRYPTO:
            # min_avg_volume is an equity SHARE-count floor (default 100k); crypto
            # Bar.volume is a COIN count (BTC trades tens of thousands of coins/day,
            # ~$30B notional), so a share floor is a category error here. Crypto
            # liquidity stays gated by SpreadRule/SlippageProtectionRule (spread_pct).
            return
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
        if ctx.order.side.is_risk_reducing or not ctx.recent_bars:
            return
        last = ctx.recent_bars[-1]
        # Anchor to the prior close (exchange circuit/LULD references are
        # prior-close based) so overnight gap moves count, and keep the
        # open-to-close measure so an intraday spike that round-trips to a
        # flat close still trips the halt.
        moves = []
        if last.open > 0:
            moves.append(abs(last.close - last.open) / last.open)
        if len(ctx.recent_bars) >= 2:
            prev_close = ctx.recent_bars[-2].close
            if prev_close > 0:
                moves.append(abs(last.close - prev_close) / prev_close)
        if not moves:
            return
        move = max(moves)
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
        if ctx.order.side.is_risk_reducing or ctx.config.news_blackout_minutes_before_econ <= 0:
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


_CLOSING_SIDES = (OrderSide.SELL, OrderSide.SELL_TO_CLOSE, OrderSide.BUY_TO_CLOSE)


def reduce_only_breach(
    order: Order,
    held_for: Callable[[str], Decimal],
    open_orders: Iterable[Order],
) -> str | None:
    """The reduce-only invariant as a pure function, so it can be enforced against
    EITHER the synced portfolio snapshot (``ReduceOnlyRule`` in the engine) or the
    broker's LIVE positions + open orders (the ``OrderManager`` submit-time
    backstop, F022). Returns a violation message, or None when the close stays
    within what is held. ``held_for(symbol)`` is the signed position quantity for
    a symbol (0 if flat); ``open_orders`` is the set resting at the broker.

    A closing order may only reduce an existing same-direction position — it can
    never exceed what is held and flip the book short."""
    open_list = list(open_orders)
    if order.legs:
        # Multi-leg option order: the parent symbol is the underlying, never
        # itself a position (public.com submits only the legs). Validate each
        # closing leg against its own contract position; opening legs are gated
        # by the size/exposure/leverage rules like any other open.
        for leg in order.legs:
            if leg.side not in _CLOSING_SIDES:
                continue
            held = held_for(leg.contract_symbol)
            available = -held if leg.side is OrderSide.BUY_TO_CLOSE else held
            # Same as the single-leg path: unfilled same-direction closing orders
            # already resting at the broker on this contract still consume the
            # closable quantity, so two spread exits can't each pass alone and
            # oversell into a short. Matched per CONTRACT symbol (open-order
            # snapshots carry the leg's contract symbol, not the underlying) and
            # by is_buy (brokers normalize *_to_close to plain buy/sell).
            contract = leg.contract_symbol.upper()
            pending = sum(
                (max(o.quantity - o.filled_quantity, Decimal(0))
                 for o in open_list
                 if o.symbol.upper() == contract
                 and o.status.is_open_at_broker
                 and o.side.is_buy == leg.side.is_buy),
                Decimal(0),
            )
            available -= pending
            available = available if available > 0 else Decimal(0)
            closing = leg.quantity * order.quantity  # ratio qty x spreads
            if closing > available:
                return (
                    f"{leg.side.value} {closing} {leg.contract_symbol} exceeds the "
                    f"closable position ({available} after {pending} already pending in "
                    "open closing orders) — the platform does not open short positions"
                )
        return None
    if order.side not in _CLOSING_SIDES:
        return None  # opening orders are gated by the size/exposure/leverage rules
    held = held_for(order.symbol)
    # BUY_TO_CLOSE covers a short (held < 0); the others close a long.
    available = -held if order.side is OrderSide.BUY_TO_CLOSE else held
    # A position only shrinks when an exit FILLS, so unfilled same-direction
    # orders still open at the broker already consume the closable quantity.
    # Without subtracting them, a resting exit (e.g. an unfilled guardian stop)
    # plus a second exit (review cycle / manual ticket) each pass alone and
    # together oversell the book into a short. Direction is matched via is_buy
    # because broker open-order snapshots normalize *_to_close sides to plain
    # buy/sell (e.g. alpaca._row_to_order).
    symbol = order.symbol.upper()
    pending = sum(
        (max(o.quantity - o.filled_quantity, Decimal(0))
         for o in open_list
         if o.symbol.upper() == symbol
         and o.status.is_open_at_broker
         and o.side.is_buy == order.side.is_buy),
        Decimal(0),
    )
    available -= pending
    available = available if available > 0 else Decimal(0)
    if order.quantity > available:
        return (
            f"{order.side.value} {order.quantity} {order.symbol} exceeds the "
            f"closable position ({available} after {pending} already pending in open "
            "closing orders) — the platform does not open short positions"
        )
    return None


class ReduceOnlyRule(RiskRule):
    """The platform does not open short positions via a plain SELL / *_to_close.
    A closing order may only reduce an existing same-direction position; it can
    never exceed what is held and flip the book short. This rule is deliberately
    NOT exempted for risk-reducing sides — it is what makes the size/exposure/
    leverage/loss-halt exemptions on those sides safe. Opening sides (BUY,
    BUY_TO_OPEN, SELL_TO_OPEN) are left to the full risk gate.

    Enforced here against the synced snapshot; the OrderManager applies the same
    invariant (via ``reduce_only_breach``) against LIVE broker state at submit as
    a backstop for the between-syncs concurrent-exit window (F022)."""

    name = "reduce_only"

    def check(self, ctx: RiskContext) -> None:
        def held_for(symbol: str) -> Decimal:
            position = ctx.portfolio.position_for(symbol)
            return position.quantity if position is not None else Decimal(0)

        msg = reduce_only_breach(ctx.order, held_for, ctx.portfolio.open_orders)
        if msg is not None:
            raise RiskViolation(self.name, msg)


ALL_RULES: list[RiskRule] = [
    FreshPortfolioRule(),
    MarketOpenRule(),
    UniverseRule(),
    ReduceOnlyRule(),
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
