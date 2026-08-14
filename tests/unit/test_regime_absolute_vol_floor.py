"""A calm market must not be labelled risk_off on a percentile alone.

Observed live (2026-08-14), SPY:

    trend=uptrend  drawdown_from_high=0.0%  realized_vol_annualized=13.5%
    vol_percentile=0.72                ->  state="risk_off"

...while the platform's own macro endpoint reported ``VIX 14.63`` and
``vix_regime: "low"`` at the same moment. Two parts of the system described one
market in opposite terms, and the PM was handed "risk_off" every cycle — which
it then cited as a reason not to open anything.

The cause is that the volatility leg was **purely relative**. `vol_percentile`
ranks today's realized vol against the trailing year, so in a quiet year the
72nd percentile of a quiet distribution is still quiet in absolute terms. 13.5%
annualized is historically low for SPY; calling it high volatility is a category
error that a percentile cannot detect by construction.

The trend and drawdown legs are already absolute and are left alone. The vol
legs now need BOTH a high percentile AND a meaningful absolute level, so
"unusually volatile *for a calm year*" stops reading as "risk off".
"""

from __future__ import annotations

import math

from poseidon.analytics.regime import compute_regime


def _series(vol_by_phase: list[tuple[int, float]], *, drift: float = 0.0004,
            seed: int = 7) -> list[float]:
    """Deterministic series with a per-phase daily volatility.

    Phases matter: a CONSTANT-vol series sits at roughly the 50th percentile by
    construction, so it can never reproduce the live condition (percentile 0.72
    at 13.5% annualized). A long calm stretch followed by a mildly less-calm one
    produces a HIGH percentile at a LOW absolute level — which is the whole bug.
    """
    rnd = 1103515245 + seed
    out = [100.0]
    for days, daily_vol in vol_by_phase:
        for _ in range(days):
            rnd = (rnd * 1103515245 + 12345) % (2**31)
            shock = ((rnd / (2**31)) - 0.5) * 2 * daily_vol * math.sqrt(3)
            out.append(out[-1] * (1 + drift + shock))
    return out


def _quiet_year_slightly_less_quiet_lately() -> list[float]:
    """~5% annualized for most of the year, ~11% recently: a high percentile at
    an absolutely calm level. This is the shape of the operator's live SPY."""
    return _series([(320, 0.003), (80, 0.007)])


def test_a_quiet_uptrend_is_not_risk_off() -> None:
    """The live case: uptrend, no drawdown, low absolute vol, high percentile."""
    r = compute_regime(_quiet_year_slightly_less_quiet_lately(), benchmark="SPY")
    assert r.trend == "uptrend"
    assert r.realized_vol_annualized is not None
    assert r.realized_vol_annualized < 0.18, "fixture should be a calm market"
    assert r.state not in {"risk_off", "stress"}, (
        f"a calm uptrend at {r.realized_vol_annualized:.1%} annualized vol was "
        f"called {r.state!r} — a percentile with no absolute anchor cannot tell "
        "'unusually volatile for a quiet year' from 'actually volatile'"
    )


def test_genuinely_high_volatility_still_reads_risk_off() -> None:
    """The guard must not blind the classifier to real volatility."""
    r = compute_regime(_series([(200, 0.006), (200, 0.030)], drift=-0.0010),
                       benchmark="SPY")
    assert r.realized_vol_annualized is not None
    assert r.realized_vol_annualized > 0.18
    assert r.state in {"risk_off", "stress"}, r.state


def test_a_downtrend_is_still_risk_off_regardless_of_vol() -> None:
    """The trend leg is absolute and must be untouched."""
    r = compute_regime(_series([(400, 0.004)], drift=-0.0030), benchmark="SPY")
    assert r.trend == "downtrend"
    assert r.state in {"risk_off", "stress"}


def test_a_deep_drawdown_is_still_stress_regardless_of_vol() -> None:
    """The drawdown leg is absolute and must be untouched."""
    closes = _series([(300, 0.004)], drift=0.0)
    closes += [closes[-1] * (1 - 0.20)]  # a 20% gap down, calm vol history
    r = compute_regime(closes, benchmark="SPY")
    assert r.drawdown_from_high >= 0.15
    assert r.state == "stress", r.detail


def test_the_report_still_carries_the_percentile() -> None:
    """The percentile remains useful context even when it no longer decides
    the state on its own — the PM and the dashboard both read it."""
    r = compute_regime(_quiet_year_slightly_less_quiet_lately(), benchmark="SPY")
    assert r.vol_percentile is not None
    assert "vol_pctile" in (r.detail or "")


def test_insufficient_history_still_reports_unknown() -> None:
    r = compute_regime([100.0, 101.0], benchmark="SPY")
    assert r.state == "unknown"
