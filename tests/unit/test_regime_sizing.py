"""Market regime classification and vol-targeted position sizing."""

from __future__ import annotations

from poseidon.analytics.regime import compute_regime
from poseidon.analytics.sizing import daily_volatility, suggest_size


def steady_series(n: int, *, start: float = 100.0, drift: float = 0.001,
                  wobble: float = 0.004, seed: int = 42) -> list[float]:
    """Deterministic (seeded) gently-trending series with realistic noise."""
    import random

    rng = random.Random(seed)
    closes = [start]
    for _ in range(1, n):
        closes.append(closes[-1] * (1 + drift + rng.uniform(-wobble, wobble)))
    return closes


class TestRegime:
    def test_insufficient_history_is_unknown(self) -> None:
        report = compute_regime([100.0] * 30, benchmark="SPY")
        assert report.state == "unknown" and report.trend == "unknown"
        assert "30 daily closes" in report.detail

    def test_steady_uptrend_is_risk_on(self) -> None:
        report = compute_regime(steady_series(300), benchmark="SPY")
        assert report.trend == "uptrend"
        assert report.state == "risk_on"
        assert report.sma_50 is not None and report.sma_200 is not None
        assert report.drawdown_from_high is not None and report.drawdown_from_high < 0.02

    def test_crash_is_stress(self) -> None:
        closes = steady_series(280)
        # Sharp 20% decline over 20 sessions: deep drawdown + vol spike.
        for _ in range(20):
            closes.append(closes[-1] * 0.989)
        report = compute_regime(closes, benchmark="SPY")
        assert report.state == "stress"
        assert report.drawdown_from_high is not None and report.drawdown_from_high > 0.15

    def test_downtrend_is_risk_off(self) -> None:
        closes = steady_series(260)
        # Sustained bleed: below both moving averages, ~14% drawdown —
        # a real downtrend that stops short of the stress threshold.
        for _ in range(60):
            closes.append(closes[-1] * 0.9975)
        report = compute_regime(closes, benchmark="SPY")
        assert report.trend == "downtrend"
        assert report.state in ("risk_off", "stress")

    def test_summary_line_reads_well(self) -> None:
        line = compute_regime(steady_series(300), benchmark="SPY").summary_line()
        assert "RISK_ON" in line and "SPY" in line and "uptrend" in line


class TestVolTargetedSizing:
    def test_daily_volatility(self) -> None:
        # Alternating ±1% days: daily vol ≈ 1%.
        closes = [100.0]
        for i in range(40):
            closes.append(closes[-1] * (1.01 if i % 2 else 0.99))
        vol = daily_volatility(closes)
        assert vol is not None and 0.009 < vol < 0.0115
        assert daily_volatility(closes[:10]) is None  # not enough history

    def test_sizing_math(self) -> None:
        # equity 100k, budget 0.5% => $500 daily risk; price 100, vol 1%
        # => $1/day per share => 500 shares... but 10% position cap = 100.
        result = suggest_size(equity=100_000, price=100, daily_vol=0.01,
                              risk_budget_pct=0.005, max_position_pct=0.10,
                              buying_power=100_000)
        assert result["uncapped_shares"] == 500.0
        assert result["suggested_shares"] == 100
        assert "max_position_pct" in result["capped_by"][0]

    def test_buying_power_caps(self) -> None:
        result = suggest_size(equity=100_000, price=100, daily_vol=0.01,
                              risk_budget_pct=0.005, max_position_pct=1.0,
                              buying_power=2_000)
        assert result["suggested_shares"] == 20
        assert "buying power" in result["capped_by"]

    def test_high_vol_shrinks_size(self) -> None:
        quiet = suggest_size(equity=100_000, price=50, daily_vol=0.008,
                             risk_budget_pct=0.005, max_position_pct=1.0,
                             buying_power=1e9)
        wild = suggest_size(equity=100_000, price=50, daily_vol=0.04,
                            risk_budget_pct=0.005, max_position_pct=1.0,
                            buying_power=1e9)
        assert wild["suggested_shares"] * 5 == quiet["suggested_shares"]
        # Both target the same dollar risk.
        assert wild["target_daily_dollar_risk"] == quiet["target_daily_dollar_risk"]

    def test_degenerate_inputs(self) -> None:
        assert "error" in suggest_size(equity=0, price=100, daily_vol=0.01,
                                       risk_budget_pct=0.005, max_position_pct=0.1,
                                       buying_power=0)
        assert "error" in suggest_size(equity=100_000, price=100, daily_vol=0.0,
                                       risk_budget_pct=0.005, max_position_pct=0.1,
                                       buying_power=0)
