"""In-memory portfolio state, refreshed by the sync service.

This is the single source of truth the risk engine and AI read from. Every
snapshot is timestamped; consumers must check ``age_seconds`` and refuse to
act on stale state (the risk engine enforces this).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ..core.models import AccountSnapshot, Dividend, Fill, Order, Position, TaxLot


class PortfolioState:
    def __init__(self) -> None:
        self.account: AccountSnapshot | None = None
        self.positions: list[Position] = []
        self.open_orders: list[Order] = []
        self.tax_lots: list[TaxLot] = []
        self.dividends: list[Dividend] = []
        self.recent_fills: list[Fill] = []
        self.synced_at: datetime | None = None
        # Rolling equity history for drawdown / loss-limit checks.
        self.equity_history: list[tuple[datetime, Decimal]] = []
        self.week_start_equity: Decimal | None = None
        self.day_start_equity: Decimal | None = None
        self.peak_equity: Decimal | None = None
        # Latest portfolio risk metrics (VaR/beta/correlation), refreshed on
        # a schedule; timestamped so consumers can enforce freshness.
        self.risk_metrics: dict[str, object] | None = None
        self.risk_metrics_at: datetime | None = None

    def risk_metrics_age_seconds(self) -> float | None:
        if self.risk_metrics_at is None:
            return None
        return (datetime.now(UTC) - self.risk_metrics_at).total_seconds()

    @property
    def age_seconds(self) -> float | None:
        if self.synced_at is None:
            return None
        return (datetime.now(UTC) - self.synced_at).total_seconds()

    @property
    def equity(self) -> Decimal | None:
        return self.account.equity if self.account else None

    def position_for(self, symbol: str) -> Position | None:
        symbol = symbol.upper()
        for p in self.positions:
            if p.symbol.upper() == symbol:
                return p
        return None

    def gross_exposure(self) -> Decimal:
        total = Decimal(0)
        for p in self.positions:
            if p.market_value is not None:
                total += abs(p.market_value)
            else:
                total += abs(p.quantity * p.avg_entry_price)
        return total

    def options_exposure(self) -> Decimal:
        total = Decimal(0)
        for p in self.positions:
            if p.asset_class.value == "option":
                total += abs(p.market_value if p.market_value is not None
                             else p.quantity * p.avg_entry_price * 100)
        return total

    def record_equity(self, equity: Decimal, at: datetime) -> None:
        self.equity_history.append((at, equity))
        # Keep a bounded window (about a month of 1-minute marks).
        if len(self.equity_history) > 45_000:
            self.equity_history = self.equity_history[-40_000:]
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

    def drawdown_pct(self) -> float:
        if not self.peak_equity or self.equity is None or self.peak_equity <= 0:
            return 0.0
        return float((self.peak_equity - self.equity) / self.peak_equity)

    def day_loss_pct(self) -> float:
        if not self.day_start_equity or self.equity is None or self.day_start_equity <= 0:
            return 0.0
        return max(0.0, float((self.day_start_equity - self.equity) / self.day_start_equity))

    def week_loss_pct(self) -> float:
        if not self.week_start_equity or self.equity is None or self.week_start_equity <= 0:
            return 0.0
        return max(0.0, float((self.week_start_equity - self.equity) / self.week_start_equity))

    def snapshot_dict(self) -> dict[str, object]:
        """JSON-safe summary for the dashboard and the AI context."""
        return {
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
            "account": self.account.model_dump(mode="json") if self.account else None,
            "positions": [p.model_dump(mode="json") for p in self.positions],
            "open_orders": [o.model_dump(mode="json") for o in self.open_orders],
            "gross_exposure": str(self.gross_exposure()),
            "options_exposure": str(self.options_exposure()),
            "drawdown_pct": self.drawdown_pct(),
            "day_loss_pct": self.day_loss_pct(),
            "week_loss_pct": self.week_loss_pct(),
        }
