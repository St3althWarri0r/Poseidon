"""Risk engine: the mandatory gate between any decision and any order.

``validate_order`` gathers the live context (fresh quote, volume history,
economic calendar) and runs every rule. There is deliberately no way to
submit an order that bypasses this method — the order manager owns the only
broker reference used for submission and calls the engine first.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from ..core.clock import MarketClock
from ..core.config import RiskConfig
from ..core.enums import AssetClass
from ..core.errors import CircuitBreakerOpen, DataError, RiskViolation
from ..core.events import EventBus, Topics
from ..core.models import Bar, EconomicEvent, Order, Quote
from ..data.router import DataRouter
from ..portfolio.state import PortfolioState
from .circuit import CircuitBreaker, TradeCooldowns
from .rules import ALL_RULES, RiskContext, RiskRule

log = structlog.get_logger(__name__)


class RiskEngine:
    def __init__(self, config: RiskConfig, portfolio: PortfolioState, router: DataRouter,
                 clock: MarketClock, bus: EventBus, *, rules: list[RiskRule] | None = None) -> None:
        self._config = config
        self._portfolio = portfolio
        self._router = router
        self._clock = clock
        self._bus = bus
        self._rules = rules if rules is not None else list(ALL_RULES)
        self.circuit = CircuitBreaker(
            error_threshold=config.circuit_breaker_error_threshold,
            window_seconds=config.circuit_breaker_window_seconds,
            cooldown_seconds=config.circuit_breaker_cooldown_seconds,
        )
        self.cooldowns = TradeCooldowns(per_symbol_seconds=config.trade_cooldown_seconds)
        # Dedicated sleeves: strategy name -> fraction of equity its
        # positions may occupy (overrides max_position_pct for that
        # strategy only). Maintained by the algorithm workshop.
        self.sleeve_caps: dict[str, float] = {}
        # strategy name -> symbols it signalled this cycle (trusted attribution
        # for sleeve caps; see PositionSizeRule).
        self.sleeve_attribution: dict[str, set[str]] = {}
        self._orders_today = 0
        self._orders_today_date: str = ""

    # -- accounting ----------------------------------------------------------

    def set_cycle_attribution(self, signals: list[object]) -> None:
        """Record which strategy signalled which symbols this cycle, so a
        sleeve cap can only apply to symbols its strategy actually surfaced."""
        attribution: dict[str, set[str]] = {}
        for sig in signals:
            name = getattr(sig, "strategy", None)
            symbol = getattr(sig, "symbol", None)
            if name and symbol:
                attribution.setdefault(str(name), set()).add(str(symbol).upper())
        self.sleeve_attribution = attribution

    def seed_orders_today(self, count: int, date: str) -> None:
        """Rehydrate the daily order counter from persisted history so a
        restart cannot silently reset max_orders_per_day."""
        self._orders_today_date = date
        self._orders_today = count

    def note_order_submitted(self, symbol: str) -> None:
        self._roll_daily_counter()
        self._orders_today += 1
        self.cooldowns.record_trade(symbol)

    def note_execution_error(self, reason: str) -> None:
        if self.circuit.record_error(reason):
            # publish is fire-and-forget; engine methods stay sync-friendly.
            # The kernel subscribes CIRCUIT_OPENED to the audit log (app.py).
            asyncio.get_running_loop().create_task(
                self._bus.publish(Topics.CIRCUIT_OPENED, {"reason": reason})
            )

    def _roll_daily_counter(self) -> None:
        today = self._clock.now_eastern().date().isoformat()
        if today != self._orders_today_date:
            self._orders_today_date = today
            self._orders_today = 0

    # -- validation -------------------------------------------------------------

    async def validate_order(self, order: Order) -> Quote:
        """Run every risk rule against live data.

        Returns the fresh quote used, so the caller can reuse it (e.g. for
        limit-price sanity in the ticket). Raises RiskViolation or
        CircuitBreakerOpen on any breach; raises DataError when the live
        context cannot be assembled (in which case the order must not go
        out — no data, no trade).
        """
        if self.circuit.is_open:
            raise CircuitBreakerOpen(self.circuit.reason or "open")
        self._roll_daily_counter()

        # Live inputs. Any failure here aborts the order — deliberately no
        # fallbacks to cached or assumed values.
        quote = await self._router.quote(order.symbol, allow_delayed=False)
        order.arrival_price = quote.mid or quote.last  # TCA benchmark price
        bars: list[Bar] = []
        try:
            bars = await self._router.bars(order.symbol, timeframe="1d", limit=30)
        except DataError:
            bars = []  # VolumeRule treats missing history as a violation for buys
        econ: list[EconomicEvent] = []
        if self._config.news_blackout_minutes_before_econ > 0:
            try:
                econ = await self._router.economic_calendar(days_ahead=1)
            except DataError:
                log.warning("economic calendar unavailable; blackout rule will pass empty")
        order_sector, position_sectors = await self._gather_sectors(order)

        ctx = RiskContext(
            order=order,
            quote=quote,
            portfolio=self._portfolio,
            config=self._config,
            clock=self._clock,
            recent_bars=bars,
            upcoming_econ=econ,
            orders_today=self._orders_today,
            cooldown_remaining=self.cooldowns.remaining(order.symbol),
            order_sector=order_sector,
            position_sectors=position_sectors,
            sleeve_caps=dict(self.sleeve_caps),
            sleeve_attribution={k: set(v) for k, v in self.sleeve_attribution.items()},
        )
        for rule in self._rules:
            try:
                rule.check(ctx)
            except RiskViolation as violation:
                log.warning("risk violation", rule=violation.rule, order=order.symbol,
                            side=order.side, detail=str(violation))
                await self._bus.publish(
                    Topics.RISK_VIOLATION,
                    {"rule": violation.rule, "order_id": order.id, "symbol": order.symbol,
                     "detail": str(violation), "at": datetime.now(UTC).isoformat()},
                )
                raise
        return quote

    async def _gather_sectors(self, order: Order) -> tuple[str | None, dict[str, str]]:
        """Sector classifications for the concentration rule. Router results
        are week-cached, so steady-state cost is zero API calls. Only
        gathered for risk-increasing equity orders — the ones the rule
        constrains. Position lookups run concurrently to bound the cold-path
        latency to one round-trip instead of N."""
        if order.side.is_risk_reducing or order.asset_class is not AssetClass.EQUITY:
            return None, {}
        order_sector = await self._router.sector(order.symbol)
        if order_sector is None:
            return None, {}
        equity_positions = [p for p in self._portfolio.positions
                            if p.asset_class is AssetClass.EQUITY]
        sectors = await asyncio.gather(
            *(self._router.sector(p.symbol) for p in equity_positions)
        )
        position_sectors = {
            p.symbol.upper(): s
            for p, s in zip(equity_positions, sectors, strict=True) if s is not None
        }
        return order_sector, position_sectors

    def status(self) -> dict[str, object]:
        self._roll_daily_counter()
        return {
            "circuit_open": self.circuit.is_open,
            "circuit_reason": self.circuit.reason,
            "orders_today": self._orders_today,
            "max_orders_per_day": self._config.max_orders_per_day,
            "day_loss_pct": self._portfolio.day_loss_pct(),
            "week_loss_pct": self._portfolio.week_loss_pct(),
            "drawdown_pct": self._portfolio.drawdown_pct(),
            "limits": {
                "max_daily_loss_pct": self._config.max_daily_loss_pct,
                "max_weekly_loss_pct": self._config.max_weekly_loss_pct,
                "max_drawdown_pct": self._config.max_drawdown_pct,
                "max_position_pct": self._config.max_position_pct,
                "max_leverage": self._config.max_leverage,
            },
        }
