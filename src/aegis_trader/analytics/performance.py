"""Performance analytics.

Builds closed round trips from the platform's own filled-order history
(FIFO per symbol), computes risk-adjusted portfolio metrics from the
stored equity marks, and attributes realized P&L to the strategy that
originated each entry. Everything derives from data Aegis recorded itself
— no re-fetching, no estimation.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.enums import OrderSide

TRADING_DAYS = 252


@dataclass
class FillRecord:
    """Minimal view of a filled order, in fill-time order."""

    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    at: datetime
    strategy: str = ""


@dataclass
class RoundTrip:
    symbol: str
    strategy: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entered_at: datetime
    exited_at: datetime

    @property
    def pnl(self) -> Decimal:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return float((self.exit_price - self.entry_price) / self.entry_price)

    @property
    def holding_days(self) -> float:
        return max((self.exited_at - self.entered_at).total_seconds() / 86400, 0.0)


def build_round_trips(fills: list[FillRecord]) -> list[RoundTrip]:
    """FIFO-match sells against prior buys per symbol. Partial fills split
    lots; sells without a matching open lot (external/imported positions)
    are skipped rather than guessed at."""
    open_lots: dict[str, deque[FillRecord]] = defaultdict(deque)
    trips: list[RoundTrip] = []
    for f in sorted(fills, key=lambda x: x.at):
        symbol = f.symbol.upper()
        if f.side.is_buy:
            open_lots[symbol].append(
                FillRecord(symbol=symbol, side=f.side, quantity=f.quantity,
                           price=f.price, at=f.at, strategy=f.strategy)
            )
            continue
        remaining = f.quantity
        lots = open_lots[symbol]
        while remaining > 0 and lots:
            lot = lots[0]
            matched = min(lot.quantity, remaining)
            trips.append(
                RoundTrip(symbol=symbol, strategy=lot.strategy or f.strategy,
                          quantity=matched, entry_price=lot.price, exit_price=f.price,
                          entered_at=lot.at, exited_at=f.at)
            )
            lot.quantity -= matched
            remaining -= matched
            if lot.quantity <= 0:
                lots.popleft()
    return trips


@dataclass
class PerformanceReport:
    # portfolio-level (from equity marks)
    total_return: float = 0.0
    cagr: float = 0.0
    annualized_volatility: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    benchmark_note: str = ""
    # trade-level (from round trips)
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    avg_holding_days: float = 0.0
    realized_pnl: float = 0.0
    monthly_returns: dict[str, float] = field(default_factory=dict)
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_return": round(self.total_return, 4),
            "cagr": round(self.cagr, 4),
            "annualized_volatility": round(self.annualized_volatility, 4),
            "sharpe": round(self.sharpe, 2),
            "sortino": round(self.sortino, 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "calmar": round(self.calmar, 2),
            "trades": self.trades,
            "win_rate": round(self.win_rate, 3),
            "profit_factor": round(self.profit_factor, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "expectancy": round(self.expectancy, 2),
            "avg_holding_days": round(self.avg_holding_days, 1),
            "realized_pnl": round(self.realized_pnl, 2),
            "monthly_returns": {k: round(v, 4) for k, v in sorted(self.monthly_returns.items())},
            "by_strategy": self.by_strategy,
        }


def _daily_closes(equity_points: list[tuple[datetime, float]]) -> list[tuple[str, float]]:
    """Collapse intraday marks to one close per calendar day (last mark wins)."""
    by_day: dict[str, float] = {}
    for at, equity in sorted(equity_points, key=lambda x: x[0]):
        by_day[at.date().isoformat()] = equity
    return sorted(by_day.items())


def compute_performance(equity_points: list[tuple[datetime, float]],
                        round_trips: list[RoundTrip],
                        *, risk_free_annual: float = 0.0) -> PerformanceReport:
    report = PerformanceReport()

    closes = _daily_closes(equity_points)
    if len(closes) >= 2 and closes[0][1] > 0:
        values = [v for _, v in closes]
        report.total_return = values[-1] / values[0] - 1
        days = max(len(values) - 1, 1)
        years = days / TRADING_DAYS
        if years > 0 and values[-1] > 0:
            report.cagr = (values[-1] / values[0]) ** (1 / max(years, 1 / TRADING_DAYS)) - 1
        rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            std = var ** 0.5
            report.annualized_volatility = std * TRADING_DAYS ** 0.5
            rf_daily = risk_free_annual / TRADING_DAYS
            if std > 0:
                report.sharpe = (mean - rf_daily) / std * TRADING_DAYS ** 0.5
            downside = [r for r in rets if r < rf_daily]
            if downside:
                dvar = sum((r - rf_daily) ** 2 for r in downside) / len(downside)
                dstd = dvar ** 0.5
                if dstd > 0:
                    report.sortino = (mean - rf_daily) / dstd * TRADING_DAYS ** 0.5
        peak, max_dd = float("-inf"), 0.0
        for v in values:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        report.max_drawdown = max_dd
        if max_dd > 0:
            report.calmar = report.cagr / max_dd
        # Monthly returns from the last close of each month.
        month_last: dict[str, float] = {}
        for day, value in closes:
            month_last[day[:7]] = value
        months = sorted(month_last.items())
        prev = values[0]
        for month, value in months:
            if prev > 0:
                report.monthly_returns[month] = value / prev - 1
            prev = value

    if round_trips:
        wins = [t for t in round_trips if t.pnl > 0]
        losses = [t for t in round_trips if t.pnl <= 0]
        gross_win = float(sum(t.pnl for t in wins))
        gross_loss = float(-sum(t.pnl for t in losses))
        report.trades = len(round_trips)
        report.win_rate = len(wins) / len(round_trips)
        report.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float(bool(gross_win)) * 99.0
        report.avg_win = gross_win / len(wins) if wins else 0.0
        report.avg_loss = -gross_loss / len(losses) if losses else 0.0
        report.realized_pnl = float(sum(t.pnl for t in round_trips))
        report.expectancy = report.realized_pnl / len(round_trips)
        report.avg_holding_days = sum(t.holding_days for t in round_trips) / len(round_trips)
        by_strategy: dict[str, list[RoundTrip]] = defaultdict(list)
        for t in round_trips:
            by_strategy[t.strategy or "unattributed"].append(t)
        for name, trips in sorted(by_strategy.items()):
            strategy_wins = sum(1 for t in trips if t.pnl > 0)
            report.by_strategy[name] = {
                "trades": len(trips),
                "win_rate": round(strategy_wins / len(trips), 3),
                "realized_pnl": round(float(sum(t.pnl for t in trips)), 2),
                "avg_holding_days": round(sum(t.holding_days for t in trips) / len(trips), 1),
            }
    return report
