"""Portfolio risk metrics (VaR/beta/correlation) and execution quality (TCA)."""

from __future__ import annotations

from decimal import Decimal

from aegis_trader.analytics.execution import execution_quality, slippage_bps
from aegis_trader.analytics.risk_metrics import compute_risk_metrics
from aegis_trader.core.enums import OrderSide


def metrics(weights, returns, bench=None, uncovered=()):  # type: ignore[no-untyped-def]
    return compute_risk_metrics(
        weights, returns, bench, benchmark="SPY",
        positions_total=len(weights) + len(uncovered), uncovered=list(uncovered),
    )


class TestRiskMetrics:
    def test_var_of_single_position(self) -> None:
        # 100 observations: ten -5% days (a 10% bad-day frequency), full weight.
        # The 5th-percentile daily loss is squarely inside the -5% tail.
        rets = [0.001] * 90 + [-0.05] * 10
        report = metrics({"AAPL": 1.0}, {"AAPL": rets})
        assert abs(report.var_95_pct - 0.05) < 1e-9
        assert report.var_99_pct >= report.var_95_pct
        assert report.expected_shortfall_95_pct >= report.var_95_pct
        assert report.observations == 100

    def test_half_weight_halves_var(self) -> None:
        rets = [0.0] * 50 + [-0.04] * 50
        full = metrics({"AAPL": 1.0}, {"AAPL": rets})
        half = metrics({"AAPL": 0.5}, {"AAPL": rets})
        assert abs(half.var_95_pct - full.var_95_pct / 2) < 1e-9

    def test_beta_against_benchmark(self) -> None:
        bench = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01] * 10
        double = [2 * r for r in bench]
        report = metrics({"TQQQ": 1.0}, {"TQQQ": double}, bench=bench)
        assert report.portfolio_beta is not None
        assert abs(report.portfolio_beta - 2.0) < 0.01

    def test_correlated_pair_detected(self) -> None:
        base = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01] * 10
        anti = [-r for r in base]
        near = [r * 1.01 for r in base]
        report = metrics(
            {"A": 0.3, "B": 0.3, "C": 0.4},
            {"A": base, "B": near, "C": anti},
        )
        assert report.most_correlated_pair == ("A", "B")
        assert report.max_pairwise_correlation is not None
        assert report.max_pairwise_correlation > 0.99

    def test_uncovered_positions_reported_not_estimated(self) -> None:
        report = metrics({"AAPL": 0.5}, {"AAPL": [0.01, -0.01] * 20},
                         uncovered=["SPXW240621C05300000"])
        assert report.uncovered_symbols == ["SPXW240621C05300000"]
        assert report.positions_covered == 1 and report.positions_total == 2

    def test_empty_portfolio_yields_zero_var(self) -> None:
        report = metrics({}, {})
        assert report.var_95_pct == 0.0 and report.observations == 0


class TestExecutionQuality:
    def test_slippage_sign_convention(self) -> None:
        # Buy filled above arrival = cost; sell filled below arrival = cost.
        assert slippage_bps(OrderSide.BUY, Decimal("100"), Decimal("100.10")) == 10.0
        assert slippage_bps(OrderSide.SELL, Decimal("100"), Decimal("99.90")) == 10.0
        assert slippage_bps(OrderSide.BUY, Decimal("100"), Decimal("99.90")) == -10.0
        assert slippage_bps(OrderSide.SELL_TO_CLOSE, Decimal("2.00"), Decimal("2.02")) == -100.0

    def test_aggregation(self) -> None:
        orders = [
            {"status": "filled", "side": "buy", "symbol": "AAPL", "slippage_bps": 5.0,
             "created_at": "2026-07-01T14:30:00+00:00", "updated_at": "2026-07-01T14:30:30+00:00"},
            {"status": "filled", "side": "sell", "symbol": "AAPL", "slippage_bps": -2.0,
             "created_at": "2026-07-01T15:00:00+00:00", "updated_at": "2026-07-01T15:00:10+00:00"},
            {"status": "filled", "side": "buy", "symbol": "MSFT", "slippage_bps": 15.0,
             "created_at": "bad", "updated_at": "2026-07-01T15:01:00+00:00"},
            {"status": "canceled", "side": "buy", "symbol": "NVDA"},
            {"status": "rejected_risk", "side": "buy", "symbol": "NVDA"},  # never reached broker
        ]
        report = execution_quality(orders)
        assert report["orders_filled"] == 3
        assert report["orders_reaching_broker"] == 4  # rejected_risk excluded
        assert report["fill_rate"] == 0.75
        assert report["orders_measured"] == 3
        assert report["avg_slippage_bps"] == 6.0
        assert report["worst_slippage_bps"] == 15.0
        assert report["worst_fill"]["symbol"] == "MSFT"
        assert report["avg_slippage_bps_by_side"] == {"buy": 10.0, "sell": -2.0}
        assert report["avg_seconds_to_fill"] == 20.0  # bad timestamp skipped

    def test_empty_history(self) -> None:
        report = execution_quality([])
        assert report["orders_filled"] == 0 and report["fill_rate"] is None
        assert report["avg_slippage_bps"] is None
