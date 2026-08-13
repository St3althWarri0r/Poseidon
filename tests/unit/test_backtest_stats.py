"""Golden values and honesty gates for the backtest stats toolkit."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta

import pytest

from poseidon.backtest.stats import (
    MonteCarloSummary,
    annualized_vol,
    bootstrap_sharpe,
    calmar,
    equal_weight_returns,
    health_band,
    max_drawdown_of_returns,
    monte_carlo_returns,
    ols_alpha_beta,
    permutation_significance,
    profit_factor,
    safe_cagr,
    sanitize_json,
    sharpe_ratio,
    sortino,
)

# -- OLS ----------------------------------------------------------------------


def _linear_fixture(n: int = 100) -> tuple[list[float], list[float]]:
    x = [0.01 * math.sin(i) for i in range(n)]
    y = [0.0002 + 0.5 * xi for xi in x]
    return y, x  # (strategy, benchmark)


def test_ols_noiseless_recovers_alpha_beta_exactly() -> None:
    y, x = _linear_fixture(100)
    r = ols_alpha_beta(y, x)
    assert r["beta"] == pytest.approx(0.5, abs=1e-9)
    assert r["alpha_daily"] == pytest.approx(0.0002, abs=1e-9)
    assert r["alpha_annual"] == pytest.approx(0.0002 * 252, abs=1e-6)
    assert r["r2"] == pytest.approx(1.0, abs=1e-9)
    assert r["n_days"] == 100
    # Residuals are float-noise tiny, so the alpha t-stat is enormous.
    assert r["t_alpha"] is None or abs(r["t_alpha"]) > 100


def test_ols_noisy_fixture_pinned_literals() -> None:
    # Literals computed independently via statistics.linear_regression plus
    # the classical OLS standard-error formulas.
    x = [0.01 * math.sin(i) for i in range(100)]
    y = [0.0003 + 0.8 * x[i] + 0.002 * math.sin(3 * i + 1) for i in range(100)]
    r = ols_alpha_beta(y, x)
    assert r["beta"] == pytest.approx(0.7980462189494019, abs=1e-9)
    assert r["alpha_daily"] == pytest.approx(0.0003138714754890718, abs=1e-9)
    assert r["t_alpha"] == pytest.approx(2.2668696227823255, abs=1e-6)
    assert r["r2"] == pytest.approx(0.9442998384988084, abs=1e-6)


def test_ols_gate_needs_more_than_60_days() -> None:
    y61, x61 = _linear_fixture(61)
    r61 = ols_alpha_beta(y61, x61)
    assert r61["beta"] is not None and r61["n_days"] == 61

    y60, x60 = _linear_fixture(60)
    r60 = ols_alpha_beta(y60, x60)
    assert r60["n_days"] == 60
    for key in ("alpha_daily", "alpha_annual", "beta", "t_alpha", "r2"):
        assert r60[key] is None


def test_ols_flat_benchmark_is_none_not_a_crash() -> None:
    y = [0.001 * math.sin(i) for i in range(100)]
    x = [0.001] * 100  # zero variance benchmark
    r = ols_alpha_beta(y, x)
    assert r["beta"] is None and r["t_alpha"] is None and r["n_days"] == 100


def test_ols_rejects_a_benchmark_whose_variance_is_only_float_noise() -> None:
    """Version-independent pin for the degenerate-benchmark guard.

    ``sxx <= 0`` was a float-equality test in disguise. For a constant series
    CPython 3.12+ compensates its summation and yields exactly 0.0, but
    earlier versions leave a ~1e-35 residue that slipped past the guard — and
    beta is then a division BY that residue, i.e. rounding noise amplified
    into a plausible-looking number. (This is precisely how the constant-x
    case above passed locally on 3.14 while failing CI on 3.11 and 3.12.)

    Rather than depend on the interpreter's summation, this constructs a
    benchmark whose variance is genuinely nonzero but negligible against its
    own scale — the same pathology, reproducible everywhere.
    """
    y = [0.001 * math.sin(i) for i in range(100)]
    x = [0.001] * 99 + [0.001 + 1e-12]
    sxx = sum((xi - sum(x) / len(x)) ** 2 for xi in x)
    assert sxx > 0, "fixture must have strictly positive variance to be a real test"
    r = ols_alpha_beta(y, x)
    assert r["beta"] is None and r["t_alpha"] is None and r["n_days"] == 100


def test_ols_still_estimates_a_genuinely_low_variance_benchmark() -> None:
    # The guard must not swallow real signal: a small but honest variation is
    # ~24 orders of magnitude above the noise floor and stays estimable.
    x = [0.001 + 1e-6 * math.sin(i) for i in range(100)]
    y = [0.0002 + 0.5 * xi for xi in x]
    r = ols_alpha_beta(y, x)
    assert r["beta"] == pytest.approx(0.5, abs=1e-6)


# -- safe_cagr ----------------------------------------------------------------


def test_safe_cagr_wipeout_rules() -> None:
    assert safe_cagr(100_000, 0, 500) == -1.0
    # Negative final equity must not raise (complex ** then round() TypeError).
    assert safe_cagr(100_000, -50, 500) == -1.0
    # -99.1% total return: never annualize a wipeout.
    assert safe_cagr(100_000, 900, 500) == -1.0
    assert safe_cagr(0, 100_000, 500) == 0.0
    assert safe_cagr(-1, 100_000, 500) == 0.0


def test_safe_cagr_hand_value() -> None:
    # Doubling over exactly two trading years -> sqrt(2) - 1.
    assert safe_cagr(100_000, 200_000, 504) == pytest.approx(2 ** 0.5 - 1, abs=1e-12)


# -- mirror-parity with analytics.performance ---------------------------------


def test_sortino_calmar_profit_factor_match_analytics_performance() -> None:
    """The stats formulas must EQUAL analytics.performance.compute_performance
    on an equivalent fixture — pins the mirrored definitions so the two
    surfaces never drift."""
    from decimal import Decimal

    from poseidon.analytics.performance import RoundTrip, compute_performance

    values = [100_000.0]
    for i in range(1, 80):
        values.append(values[-1] * (1 + 0.0008 + 0.012 * math.sin(i / 3)))
    day0 = datetime(2025, 1, 6, 16, 0, tzinfo=UTC)  # a Monday
    points: list[tuple[datetime, float]] = []
    day = day0
    for v in values:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        points.append((day, v))
        day += timedelta(days=1)
    trips = [
        RoundTrip(symbol="A", strategy="s", quantity=Decimal("10"),
                  entry_price=Decimal("100"), exit_price=Decimal("110"),
                  entered_at=day0, exited_at=day0 + timedelta(days=5)),
        RoundTrip(symbol="B", strategy="s", quantity=Decimal("10"),
                  entry_price=Decimal("100"), exit_price=Decimal("97.5"),
                  entered_at=day0, exited_at=day0 + timedelta(days=9)),
        RoundTrip(symbol="C", strategy="s", quantity=Decimal("4"),
                  entry_price=Decimal("50"), exit_price=Decimal("56"),
                  entered_at=day0, exited_at=day0 + timedelta(days=2)),
    ]
    report = compute_performance(points, trips)

    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    assert annualized_vol(rets) == report.annualized_volatility
    assert sortino(rets) == report.sortino
    assert sharpe_ratio(rets) == report.sharpe
    # Same formula fed performance's own cagr: pins calmar = cagr / max_dd.
    assert calmar(report.cagr, report.max_drawdown) == report.calmar
    pnls = [float(t.pnl) for t in trips]
    assert profit_factor(pnls) == report.profit_factor


def test_profit_factor_no_loss_cap_is_99_never_inf() -> None:
    assert profit_factor([10.0, 5.0]) == 99.0
    assert profit_factor([]) == 0.0
    assert profit_factor([0.0, 0.0]) == 0.0
    assert profit_factor([10.0, -5.0]) == 2.0


# -- permutation significance -------------------------------------------------


def test_significance_constant_positive_returns_is_significant() -> None:
    r = permutation_significance([0.005] * 100, runs=200, seed=1)
    assert r is not None
    assert r["p_value_sharpe"] <= 0.02
    assert r["method_sharpe"] == "sign_flip"
    assert r["method_maxdd"] == "order_permutation"
    assert r["runs"] == 200 and r["seed"] == 1


def test_significance_symmetric_zero_mean_is_not_significant() -> None:
    rets = [0.01, -0.01] * 50
    r = permutation_significance(rets, runs=400, seed=2)
    assert r is not None
    assert 0.2 <= float(r["p_value_sharpe"]) <= 0.8


def test_significance_maxdd_discriminates_orderings_of_same_multiset() -> None:
    front = [-0.02] * 15 + [0.01] * 45  # losses clustered up front
    benign: list[float] = []
    for i in range(45):
        benign.append(0.01)
        if i % 3 == 0:
            benign.append(-0.02)
    assert sorted(front) == sorted(benign)  # same multiset
    r_front = permutation_significance(front, runs=300, seed=3)
    r_benign = permutation_significance(benign, runs=300, seed=3)
    assert r_front is not None and r_benign is not None
    assert r_front["p_value_maxdd"] < r_benign["p_value_maxdd"]


def test_significance_gates_and_determinism() -> None:
    assert permutation_significance([0.01] * 19, runs=100, seed=1) is None
    assert permutation_significance([0.01] * 100, runs=0, seed=1) is None
    rets = [0.01 * math.sin(i) + 0.001 for i in range(60)]
    a = permutation_significance(rets, runs=150, seed=9)
    b = permutation_significance(rets, runs=150, seed=9)
    assert a == b and a is not None
    # The add-one convention keeps p strictly positive.
    assert float(a["p_value_sharpe"]) > 0 and float(a["p_value_maxdd"]) > 0


# -- bootstrap ----------------------------------------------------------------


def test_bootstrap_sharpe_ci_brackets_observed() -> None:
    rets = [0.001 + 0.01 * math.sin(i / 5) for i in range(100)]
    r = bootstrap_sharpe(rets, runs=300, seed=7)
    assert r is not None
    lo, hi = r["sharpe_ci_95"]
    observed = sharpe_ratio(rets)
    assert lo <= observed <= hi
    assert 0 <= r["prob_sharpe_positive"] <= 1
    assert bootstrap_sharpe(rets, runs=300, seed=7) == r  # seed determinism
    assert bootstrap_sharpe([0.01] * 19, runs=100, seed=1) is None
    assert bootstrap_sharpe(rets, runs=0, seed=1) is None


# -- Monte Carlo move parity --------------------------------------------------


def _fixed_result():  # noqa: ANN202
    from poseidon.backtest.engine import BacktestResult

    rets = [0.001 + 0.01 * math.sin(i / 9) for i in range(1, 121)]
    curve = []
    equity = 100_000.0
    day = datetime(2025, 1, 2, tzinfo=UTC).date()
    curve.append((day, equity))
    for r in rets:
        day = day + timedelta(days=1)
        equity *= 1 + r
        curve.append((day, equity))
    return BacktestResult(equity_curve=curve)


def test_monte_carlo_returns_parity_with_pre_move_literal() -> None:
    """Pinned from the CURRENT analysis.monte_carlo (runs=200, seed=42) BEFORE
    the computation moved to stats.py — proves the move is behavior-preserving."""
    result = _fixed_result()
    expected = MonteCarloSummary(runs=200, median_return=0.1592, p05_return=0.0067,
                                 p95_return=0.3137, median_max_drawdown=0.0463,
                                 p95_max_drawdown=0.0781, prob_loss=0.045)
    assert monte_carlo_returns(result.daily_returns, runs=200, seed=42) == expected

    from poseidon.backtest.analysis import monte_carlo

    assert monte_carlo(result, runs=200, seed=42) == expected  # delegate parity


def test_monte_carlo_returns_needs_20_days() -> None:
    with pytest.raises(ValueError, match="20"):
        monte_carlo_returns([0.01] * 19, runs=10, seed=1)


# -- sanitize -----------------------------------------------------------------


def test_sanitize_json_nulls_non_finite_and_counts() -> None:
    payload = {
        "a": float("nan"),
        "b": [1.5, float("inf"), {"c": float("-inf"), "d": "text"}],
        "e": 7,
        "f": True,
        "g": (2.0, 3),
    }
    clean, replaced = sanitize_json(payload)
    assert replaced == 3
    assert clean["a"] is None
    assert clean["b"][1] is None
    assert clean["b"][2]["c"] is None
    assert clean["b"][2]["d"] == "text"
    assert clean["e"] == 7 and clean["f"] is True
    assert clean["g"] == [2.0, 3]
    json.dumps(clean, allow_nan=False)  # strict JSON clean


# -- health band --------------------------------------------------------------


def test_health_band_edges_and_reason_constants() -> None:
    assert health_band(0.5, 0.1) == {"band": "at_risk", "reasons": ["sharpe<=0.5"]}
    assert health_band(0.51, 0.39) == {"band": "healthy", "reasons": []}
    assert health_band(0.51, 0.40) == {"band": "at_risk", "reasons": ["max_drawdown>=40%"]}
    assert health_band(0.2, 0.6) == {
        "band": "at_risk", "reasons": ["sharpe<=0.5", "max_drawdown>=40%"],
    }


# -- equal-weight benchmark ---------------------------------------------------


def test_equal_weight_returns_golden_with_gap_day() -> None:
    d1, d2, d3 = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)
    closes = {
        "A": {d1: 100.0, d2: 110.0, d3: 121.0},
        "B": {d1: 200.0, d3: 210.0},  # gap: B does not print on d2
    }
    rets = equal_weight_returns(closes, [d1, d2, d3])
    # Both pairs fall back to A alone — B never prints on both days of a pair.
    assert rets == pytest.approx([0.10, 0.10])

    both = {"A": {d1: 100.0, d2: 110.0}, "B": {d1: 100.0, d2: 90.0}}
    assert equal_weight_returns(both, [d1, d2]) == pytest.approx([0.0])
    assert equal_weight_returns({}, [d1, d2]) == [0.0]


# -- misc helpers -------------------------------------------------------------


def test_max_drawdown_of_returns() -> None:
    assert max_drawdown_of_returns([0.1, -0.5, 0.2]) == pytest.approx(0.5)
    assert max_drawdown_of_returns([]) == 0.0
    assert max_drawdown_of_returns([0.01, 0.02]) == 0.0
