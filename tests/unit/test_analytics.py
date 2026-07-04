"""Performance analytics: round trips, metrics, attribution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.analytics.performance import (
    FillRecord,
    build_round_trips,
    compute_performance,
)
from poseidon.core.enums import OrderSide

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def fill(symbol: str, side: OrderSide, qty: str, price: str, days: int,
         strategy: str = "momentum") -> FillRecord:
    return FillRecord(symbol=symbol, side=side, quantity=Decimal(qty),
                      price=Decimal(price), at=T0 + timedelta(days=days),
                      strategy=strategy)


class TestRoundTrips:
    def test_simple_round_trip(self) -> None:
        trips = build_round_trips([
            fill("AAPL", OrderSide.BUY, "10", "100", 0),
            fill("AAPL", OrderSide.SELL, "10", "110", 5),
        ])
        assert len(trips) == 1
        t = trips[0]
        assert t.pnl == Decimal("100")
        assert abs(t.return_pct - 0.10) < 1e-9
        assert t.holding_days == 5
        assert t.strategy == "momentum"

    def test_fifo_partial_matching(self) -> None:
        trips = build_round_trips([
            fill("AAPL", OrderSide.BUY, "10", "100", 0, strategy="a"),
            fill("AAPL", OrderSide.BUY, "10", "120", 1, strategy="b"),
            fill("AAPL", OrderSide.SELL, "15", "130", 2),
        ])
        assert len(trips) == 2
        assert trips[0].quantity == Decimal("10") and trips[0].entry_price == Decimal("100")
        assert trips[1].quantity == Decimal("5") and trips[1].entry_price == Decimal("120")
        assert trips[0].strategy == "a" and trips[1].strategy == "b"

    def test_sell_without_open_lot_skipped(self) -> None:
        trips = build_round_trips([fill("AAPL", OrderSide.SELL, "10", "100", 0)])
        assert trips == []


class TestMetrics:
    def _equity(self, values: list[float]) -> list[tuple[datetime, float]]:
        return [(T0 + timedelta(days=i), v) for i, v in enumerate(values)]

    def test_portfolio_metrics(self) -> None:
        # 100k -> 110k with a dip to 95k: positive return, known drawdown.
        values = [100_000, 105_000, 95_000, 102_000, 110_000]
        report = compute_performance(self._equity(values), [])
        assert abs(report.total_return - 0.10) < 1e-9
        assert abs(report.max_drawdown - (105_000 - 95_000) / 105_000) < 1e-9
        assert report.annualized_volatility > 0
        assert report.monthly_returns  # at least one month bucket

    def test_trade_metrics_and_attribution(self) -> None:
        trips = build_round_trips([
            fill("AAPL", OrderSide.BUY, "10", "100", 0, "momentum"),
            fill("AAPL", OrderSide.SELL, "10", "110", 2),      # +100
            fill("MSFT", OrderSide.BUY, "10", "200", 1, "swing"),
            fill("MSFT", OrderSide.SELL, "10", "195", 3),      # -50
        ])
        report = compute_performance([], trips)
        assert report.trades == 2
        assert report.win_rate == 0.5
        assert report.realized_pnl == 50.0
        assert report.profit_factor == 2.0  # 100 gross win / 50 gross loss
        assert report.expectancy == 25.0
        assert report.by_strategy["momentum"]["realized_pnl"] == 100.0
        assert report.by_strategy["swing"]["realized_pnl"] == -50.0
        assert report.by_strategy["swing"]["win_rate"] == 0.0

    def test_sharpe_positive_for_steady_gains(self) -> None:
        values = [100_000 * (1.001 ** i) for i in range(60)]
        report = compute_performance(self._equity(values), [])
        assert report.sharpe > 1
        assert report.sortino >= 0  # no down days -> sortino left at default/0
        assert report.max_drawdown == 0.0

    def test_empty_inputs_safe(self) -> None:
        report = compute_performance([], [])
        assert report.total_return == 0.0 and report.trades == 0
        assert report.as_dict()["by_strategy"] == {}
