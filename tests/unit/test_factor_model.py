"""Fama-French three-factor attribution (r3 rank 2).

The point of this layer is to tell a strategy that merely holds a size or
value tilt apart from one with genuine residual alpha. The tests are built as
CONSTRUCTED fixtures with known answers: a pure factor bet must show its
loading and ~zero alpha, and a constant excess return must show alpha and
~zero loadings.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from poseidon.backtest.factor_model import align_to_factors, attribute
from poseidon.backtest.stats import multi_ols
from poseidon.data.famafrench import FactorRow


def _rows(n: int = 120) -> list[FactorRow]:
    """Deterministic, non-collinear factor series."""
    out: list[FactorRow] = []
    day = date(2025, 1, 1)
    for i in range(n):
        out.append(FactorRow(
            day=day + timedelta(days=i),
            mkt_rf=0.010 * math.sin(i / 3.0),
            smb=0.006 * math.cos(i / 5.0),
            hml=0.004 * math.sin(i / 7.0 + 1.0),
            rf=0.0001,
        ))
    return out


# ------------------------------------------------------------------ multi_ols


def test_multi_ols_recovers_known_coefficients_exactly() -> None:
    rows = _rows(120)
    xs = [[r.mkt_rf for r in rows], [r.smb for r in rows], [r.hml for r in rows]]
    y = [0.0003 + 1.1 * xs[0][i] - 0.4 * xs[1][i] + 0.7 * xs[2][i] for i in range(120)]
    fit = multi_ols(y, xs)
    assert fit is not None
    assert fit["alpha_daily"] == pytest.approx(0.0003, abs=1e-12)
    assert fit["betas"] == pytest.approx([1.1, -0.4, 0.7], abs=1e-9)
    assert fit["r2"] == pytest.approx(1.0, abs=1e-9)
    assert fit["n_days"] == 120


def test_multi_ols_matches_single_regressor_ols() -> None:
    # One regressor through the multi path must equal the dedicated
    # simple-OLS path, or the two surfaces can silently disagree.
    from poseidon.backtest.stats import ols_alpha_beta

    x = [0.01 * math.sin(i) for i in range(100)]
    y = [0.0002 + 0.5 * xi + 0.001 * math.cos(3 * i) for i, xi in enumerate(x)]
    simple = ols_alpha_beta(y, x)
    multi = multi_ols(y, [x])
    assert multi is not None
    assert multi["alpha_daily"] == pytest.approx(simple["alpha_daily"], abs=1e-12)
    assert multi["betas"][0] == pytest.approx(simple["beta"], abs=1e-12)
    assert multi["r2"] == pytest.approx(simple["r2"], abs=1e-12)
    assert multi["t_alpha"] == pytest.approx(simple["t_alpha"], abs=1e-9)


def test_multi_ols_rejects_collinear_regressors() -> None:
    # An exact duplicate column makes the normal equations singular; the
    # honest answer is None, not arbitrary coefficients from a near-zero pivot.
    x = [0.01 * math.sin(i) for i in range(100)]
    assert multi_ols([0.001] * 100, [x, list(x)]) is None


def test_multi_ols_honesty_gates() -> None:
    x = [0.01 * math.sin(i) for i in range(100)]
    assert multi_ols([0.001] * 60, [x[:60]]) is None      # under the 61-day floor
    assert multi_ols([0.001] * 100, []) is None           # no regressors
    assert multi_ols([0.001] * 100, [x[:99]]) is None     # ragged shapes


# ---------------------------------------------------------------- alignment


def test_alignment_is_an_inner_join_on_dates_not_a_positional_zip() -> None:
    rows = _rows(10)
    # Strategy traded only 4 of the 10 factor days, and one day the factors
    # do not cover at all.
    returns = {rows[1].day: 0.01, rows[4].day: -0.02, rows[7].day: 0.03,
               rows[9].day: 0.004, date(2030, 1, 1): 9.99}
    excess, columns = align_to_factors(returns, rows)
    assert len(excess) == 4
    assert all(len(col) == 4 for col in columns)
    # Values must come from the MATCHING day, not position 0..3.
    assert excess[0] == pytest.approx(0.01 - rows[1].rf)
    assert columns[0][0] == pytest.approx(rows[1].mkt_rf)
    assert columns[0][1] == pytest.approx(rows[4].mkt_rf)


def test_a_day_the_strategy_did_not_trade_is_not_a_zero_return() -> None:
    rows = _rows(120)
    partial = {r.day: 0.001 for r in rows[:80]}
    excess, _ = align_to_factors(partial, rows)
    assert len(excess) == 80  # the other 40 are absent, not zeros


# -------------------------------------------------------------- attribution


def test_a_pure_market_bet_shows_its_loading_and_no_alpha() -> None:
    rows = _rows(150)
    # 1.3x the market, financed at the risk-free rate: all beta, zero skill.
    returns = {r.day: 1.3 * r.mkt_rf + r.rf for r in rows}
    result = attribute(returns, rows)
    assert result is not None
    assert result.loadings["mkt_rf"] == pytest.approx(1.3, abs=1e-6)
    assert result.loadings["smb"] == pytest.approx(0.0, abs=1e-6)
    assert abs(result.alpha_annual) < 1e-6  # the honest verdict: no alpha


def test_a_size_tilt_is_attributed_to_smb_not_to_alpha() -> None:
    # The headline case: a strategy that just holds small caps would score
    # positive alpha against a single SPY benchmark. Here it must not.
    rows = _rows(150)
    returns = {r.day: 0.9 * r.mkt_rf + 0.8 * r.smb + r.rf for r in rows}
    result = attribute(returns, rows)
    assert result is not None
    assert result.loadings["smb"] == pytest.approx(0.8, abs=1e-6)
    assert abs(result.alpha_annual) < 1e-6


def test_genuine_residual_alpha_survives_the_regression() -> None:
    rows = _rows(150)
    daily_alpha = 0.0004
    returns = {r.day: daily_alpha + 1.0 * r.mkt_rf + r.rf for r in rows}
    result = attribute(returns, rows)
    assert result is not None
    assert result.alpha_daily == pytest.approx(daily_alpha, abs=1e-9)
    assert result.alpha_annual == pytest.approx(daily_alpha * 252, abs=1e-6)


def test_insufficient_overlap_returns_none_not_a_zeroed_result() -> None:
    rows = _rows(150)
    returns = {r.day: 0.001 for r in rows[:30]}  # only 30 overlapping days
    assert attribute(returns, rows) is None


def test_payload_names_the_model_and_disclaims_signal_use() -> None:
    rows = _rows(150)
    returns = {r.day: 0.0004 + r.mkt_rf + r.rf for r in rows}
    result = attribute(returns, rows)
    assert result is not None
    payload = result.as_dict()
    assert payload["model"] == "fama_french_3"
    assert set(payload["loadings"]) == {"mkt_rf", "smb", "hml"}
    assert "residual" in payload["note"].lower()
