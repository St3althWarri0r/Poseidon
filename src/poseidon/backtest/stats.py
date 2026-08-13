"""Pure-python statistics toolkit for backtest evaluation.

Everything here operates on plain ``list[float]`` daily returns (or scalar
equity values) and uses only the standard library — no numpy/scipy, and no
imports from ``poseidon.research`` (the research-isolation suite forbids
them). These are OFFLINE backtest artifacts: float arithmetic is correct
here under invariant 3's carve-out — Decimal→float conversion happens at
the existing ``float(bar.close)`` boundary and nothing flows back into
live money paths.

Formula provenance:
  * sortino / calmar / profit_factor mirror ``analytics/performance.py``
    verbatim (target-downside-deviation with the ``(n-1)`` convention, the
    99.0 no-loss profit-factor cap) so the two surfaces never drift.
  * the linear-interpolation percentile mirrors
    ``analytics/risk_metrics.py``.
  * ``MonteCarloSummary``/``monte_carlo_returns`` moved here unchanged
    from ``backtest/analysis.py`` (behavior-preserving; pinned by a
    parity literal captured before the move).
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any

_TRADING_DAYS = 252.0


# -- core ratios --------------------------------------------------------------


def sharpe_ratio(rets: list[float], *, risk_free_annual: float = 0.0) -> float:
    """Annualized Sharpe over ``risk_free_annual``; 0.0 when undefined (n<2 or
    zero variance).

    ``risk_free_annual`` defaults to 0.0 so existing callers are unchanged, but
    leaving it there overstates every result: a portfolio merely matching
    T-bills scores a healthy positive instead of the honest zero. Pass a real
    rate — ``data.treasury.risk_free_annual_on`` serves the 3-month par yield —
    wherever the number is read as strategy quality.
    """
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std: float = var ** 0.5
    rf_daily = risk_free_annual / _TRADING_DAYS
    return float((mean - rf_daily) / std * _TRADING_DAYS ** 0.5) if std > 0 else 0.0


def annualized_vol(rets: list[float]) -> float:
    """Annualized volatility, (n-1) convention — mirrors analytics/performance."""
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std: float = var ** 0.5
    return float(std * _TRADING_DAYS ** 0.5)


def sortino(rets: list[float], *, risk_free_annual: float = 0.0) -> float:
    """Target-downside-deviation Sortino mirroring analytics/performance:
    squared shortfalls divided by the TOTAL sample (n-1) — days at or above the
    target count as zero shortfall. Undefined -> 0.0 (same convention as
    sharpe).

    The target is ``risk_free_annual / 252``, not zero: a day returning less
    than cash IS a shortfall even when it is positive.
    """
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    rf_daily = risk_free_annual / _TRADING_DAYS
    downside = [r for r in rets if r < rf_daily]
    if not downside:
        return 0.0
    dvar = sum((r - rf_daily) ** 2 for r in downside) / (len(rets) - 1)
    dstd: float = dvar ** 0.5
    if dstd <= 0:
        return 0.0
    return float((mean - rf_daily) / dstd * _TRADING_DAYS ** 0.5)


def calmar(cagr: float, max_dd: float) -> float:
    """CAGR / max drawdown; 0.0 when max_dd == 0 — mirrors analytics/performance."""
    if max_dd <= 0:
        return 0.0
    return cagr / max_dd


def profit_factor(pnls: list[float]) -> float:
    """Gross wins / gross losses; no losses -> float(bool(gross_win)) * 99.0
    (never Inf) — mirrors analytics/performance."""
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = float(sum(wins))
    gross_loss = float(-sum(losses))
    if gross_loss > 0:
        return gross_win / gross_loss
    return float(bool(gross_win)) * 99.0


def max_drawdown_of_returns(rets: list[float]) -> float:
    """Max drawdown of the compounded equity walk of a return series."""
    equity = peak = 1.0
    worst = 0.0
    for r in rets:
        equity *= 1 + r
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def safe_cagr(first: float, last: float, trading_days: int) -> float:
    """CAGR with the wipeout rule: ``last <= 0`` or total return <= -99%
    returns -1.0 flat — never annualize a wipeout, never fractional-power a
    negative number (a negative base to a fractional exponent is complex and
    ``round()`` on it raises TypeError). ``first <= 0`` -> 0.0 (no basis)."""
    if first <= 0:
        return 0.0
    if last <= 0 or last / first - 1 <= -0.99:
        return -1.0
    years = max(trading_days / _TRADING_DAYS, 1 / _TRADING_DAYS)
    return float((last / first) ** (1 / years) - 1)


# -- benchmark regression -----------------------------------------------------


def ols_alpha_beta(strategy_rets: list[float],
                   benchmark_rets: list[float]) -> dict[str, float | int | None]:
    """Daily-return OLS of strategy on benchmark. Honesty gate: fewer than 61
    aligned pairs (the plan's '>60 trading days') -> all-None fields with only
    ``n_days`` populated. A zero-variance benchmark (Sxx <= 0) is equally
    unestimable -> all None."""
    n = min(len(strategy_rets), len(benchmark_rets))
    empty: dict[str, float | int | None] = {
        "alpha_daily": None, "alpha_annual": None, "beta": None,
        "t_alpha": None, "r2": None, "n_days": n,
    }
    if n < 61:
        return empty
    y = strategy_rets[-n:]
    x = benchmark_rets[-n:]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    sxx = sum((xi - mean_x) ** 2 for xi in x)
    # A benchmark with no real variation is unestimable — but `sxx <= 0` is a
    # float-equality test in disguise. Exact zero only survives when the
    # interpreter's summation happens to be exact: CPython 3.12+ compensates
    # (Neumaier) and yields 0.0 for a constant series, while earlier versions
    # leave a ~1e-35 residue. Dividing by that residue turns pure rounding
    # noise into a plausible-looking beta. Gate on sxx being negligible
    # RELATIVE to the series' own scale, which is version-independent: a
    # constant series lands near 1e-31, any real series is many orders above.
    scale = sum(xi * xi for xi in x)
    if sxx <= 0 or (scale > 0 and sxx / scale < 1e-14):
        return empty
    sxy = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    syy = sum((yi - mean_y) ** 2 for yi in y)
    beta = sxy / sxx
    alpha = mean_y - beta * mean_x
    ssr = sum((y[i] - alpha - beta * x[i]) ** 2 for i in range(n))
    s2 = ssr / (n - 2)
    se_alpha = (s2 * (1 / n + mean_x ** 2 / sxx)) ** 0.5
    t_alpha = alpha / se_alpha if se_alpha > 0 else None
    r2 = 1 - ssr / syy if syy > 0 else None
    return {
        "alpha_daily": alpha, "alpha_annual": alpha * _TRADING_DAYS, "beta": beta,
        "t_alpha": t_alpha, "r2": r2, "n_days": n,
    }


# -- significance -------------------------------------------------------------


def _directional_sharpe(rets: list[float]) -> float:
    """Sharpe for null-comparison purposes: a zero-variance series with a
    nonzero mean ranks at +-inf (a constant positive return should beat every
    randomized draw), unlike the 0.0 reporting convention of sharpe_ratio."""
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    std: float = var ** 0.5
    if std > 0:
        return float(mean / std * _TRADING_DAYS ** 0.5)
    if mean > 0:
        return math.inf
    if mean < 0:
        return -math.inf
    return 0.0


def permutation_significance(rets: list[float], *, runs: int,
                             seed: int) -> dict[str, float | int | str] | None:
    """Randomization p-values for Sharpe and max drawdown.

    Sharpe is ORDER-INVARIANT — mean and std are computed from the multiset of
    daily returns, so any order permutation leaves Sharpe unchanged and cannot
    test it. The Sharpe null is therefore SIGN-FLIP randomization: under the
    null hypothesis that daily returns are symmetric about zero (no directional
    edge), each return's sign is a fair coin; flipping signs independently
    generates the null distribution. Max drawdown IS path-dependent, so the
    honest null for drawdown clustering is the ORDER permutation of the same
    multiset. Both use the add-one convention p = (1 + hits) / (runs + 1), so
    p is never exactly 0. Returns None below 20 observations or with runs<=0.

    Deliberately takes NO risk-free rate. These p-values answer "is the edge
    distinguishable from zero", and the sign-flip null is symmetry about zero;
    subtracting a rate would silently redefine the null to "beats cash", which
    is a different question and would need its own null construction.
    """
    if len(rets) < 20 or runs <= 0:
        return None
    rng = random.Random(seed)
    observed_sharpe = _directional_sharpe(rets)
    observed_dd = max_drawdown_of_returns(rets)
    sharpe_hits = 0
    for _ in range(runs):
        flipped = [r * rng.choice((-1.0, 1.0)) for r in rets]
        if _directional_sharpe(flipped) >= observed_sharpe:
            sharpe_hits += 1
    dd_hits = 0
    pool = list(rets)
    for _ in range(runs):
        rng.shuffle(pool)
        if max_drawdown_of_returns(pool) >= observed_dd:
            dd_hits += 1
    return {
        "p_value_sharpe": round((1 + sharpe_hits) / (runs + 1), 4),
        "p_value_maxdd": round((1 + dd_hits) / (runs + 1), 4),
        "method_sharpe": "sign_flip",
        "method_maxdd": "order_permutation",
        "runs": runs,
        "seed": seed,
    }


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile on pre-sorted data (q in [0, 1]) —
    same formula shape as analytics/risk_metrics."""
    if not sorted_values:
        return 0.0
    idx = q * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def bootstrap_sharpe(rets: list[float], *, runs: int, seed: int,
                     risk_free_annual: float = 0.0) -> dict[str, Any] | None:
    """IID bootstrap (resample daily returns with replacement) of the Sharpe
    ratio: 95% CI plus the probability the resampled Sharpe is positive.
    Returns None below 20 observations or with runs<=0.

    ``risk_free_annual`` MUST match the rate used for the headline Sharpe this
    interval brackets. Resampling at rf=0 beside an rf-adjusted headline puts
    the reported point estimate outside its own confidence interval.
    """
    if len(rets) < 20 or runs <= 0:
        return None
    rng = random.Random(seed)
    n = len(rets)
    sharpes: list[float] = []
    for _ in range(runs):
        sample = [rng.choice(rets) for _ in range(n)]
        sharpes.append(sharpe_ratio(sample, risk_free_annual=risk_free_annual))
    sharpes.sort()
    return {
        "sharpe_ci_95": [round(_percentile(sharpes, 0.025), 4),
                         round(_percentile(sharpes, 0.975), 4)],
        "prob_sharpe_positive": round(sum(1 for s in sharpes if s > 0) / runs, 3),
        "runs": runs,
        "seed": seed,
    }


# -- Monte Carlo (moved verbatim from backtest/analysis.py) -------------------


@dataclass
class MonteCarloSummary:
    runs: int
    median_return: float
    p05_return: float
    p95_return: float
    median_max_drawdown: float
    p95_max_drawdown: float
    prob_loss: float


def monte_carlo_returns(rets: list[float], *, runs: int = 1000,
                        seed: int | None = None) -> MonteCarloSummary:
    """Bootstrap-resample daily returns to estimate the distribution of
    outcomes and tail drawdowns. Raises ValueError below 20 observations —
    callers that must not raise pre-check the gate."""
    if len(rets) < 20:
        raise ValueError("need at least 20 daily returns for Monte Carlo")
    rng = random.Random(seed)
    horizon = len(rets)
    finals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(runs):
        equity, peak, max_dd = 1.0, 1.0, 0.0
        for _ in range(horizon):
            equity *= 1 + rng.choice(rets)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
        finals.append(equity - 1)
        drawdowns.append(max_dd)
    finals.sort()
    drawdowns.sort()

    def pct(sorted_values: list[float], p: float) -> float:
        idx = min(int(p * len(sorted_values)), len(sorted_values) - 1)
        return sorted_values[idx]

    return MonteCarloSummary(
        runs=runs,
        median_return=round(statistics.median(finals), 4),
        p05_return=round(pct(finals, 0.05), 4),
        p95_return=round(pct(finals, 0.95), 4),
        median_max_drawdown=round(statistics.median(drawdowns), 4),
        p95_max_drawdown=round(pct(drawdowns, 0.95), 4),
        prob_loss=round(sum(1 for f in finals if f < 0) / runs, 3),
    )


# -- reporting helpers --------------------------------------------------------


def health_band(sharpe: float, max_dd: float) -> dict[str, Any]:
    """Coarse health classification for the report header."""
    reasons: list[str] = []
    if sharpe <= 0.5:
        reasons.append("sharpe<=0.5")
    if max_dd >= 0.40:
        reasons.append("max_drawdown>=40%")
    return {"band": "at_risk" if reasons else "healthy", "reasons": reasons}


def sanitize_json(obj: Any) -> tuple[Any, int]:
    """Recursively replace non-finite floats (NaN/+-Inf) with None so the
    payload always serializes as strict JSON. Returns (sanitized, replaced
    count); tuples become lists (JSON arrays either way)."""
    count = 0

    def walk(node: Any) -> Any:
        nonlocal count
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list | tuple):
            return [walk(v) for v in node]
        if isinstance(node, float) and not math.isfinite(node):
            count += 1
            return None
        return node

    return walk(obj), count


def equal_weight_returns(closes_by_day: dict[str, dict[date, float]],
                         days: list[date]) -> list[float]:
    """Equal-weight universe daily returns aligned to consecutive ``days``
    pairs: for each pair, the mean of c_t/c_{t-1}-1 over symbols printing on
    BOTH days (0.0 when none do, keeping alignment)."""
    rets: list[float] = []
    for i in range(1, len(days)):
        prev_day, day = days[i - 1], days[i]
        moves: list[float] = []
        for closes in closes_by_day.values():
            prev = closes.get(prev_day)
            cur = closes.get(day)
            if prev is not None and cur is not None and prev > 0:
                moves.append(cur / prev - 1)
        rets.append(sum(moves) / len(moves) if moves else 0.0)
    return rets
