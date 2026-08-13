"""Fama-French attribution — is the alpha real, or just factor exposure?

Poseidon's existing benchmark regression (``stats.ols_alpha_beta``) answers
"did this beat SPY". That is one regressor, so any strategy tilted toward
small caps or value scores alpha for holding a well-documented risk premium
that has been publishable since 1993.

Regressing excess returns on the canonical **Mkt-RF / SMB / HML** factors
answers the sharper question: *what is left after market, size and value
exposure are accounted for?* That residual is the only alpha worth calling
alpha, and it is materially harder to earn than the single-benchmark kind.

Two deliberate choices:

  * **Ken French's own ``RF`` is used for excess returns here** — not the
    Treasury rate that :mod:`poseidon.data.treasury` serves for Sharpe. ``Mkt-RF``
    was *constructed* by subtracting that specific RF, so internal consistency
    matters more than absolute accuracy inside the regression. (The RF column's
    1bp/day quantization is second-order against ~1% daily factor moves; it is
    NOT good enough for a Sharpe, which is why the two sources differ.)
  * **Dates are inner-joined, never index-aligned.** The factor series skips US
    market holidays and the strategy series may skip more; lining two return
    series up positionally would silently pair different days and manufacture a
    correlation that does not exist.

Offline evaluation only — nothing here touches the live money path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..data.famafrench import FactorRow
from .stats import multi_ols

#: Regressor order is fixed and reported alongside the coefficients so a
#: caller can never mis-map a loading to the wrong factor.
FACTOR_NAMES = ("mkt_rf", "smb", "hml")


@dataclass(frozen=True)
class FactorAttribution:
    """Residual alpha and factor loadings for a return series."""

    alpha_daily: float
    alpha_annual: float
    t_alpha: float | None
    loadings: dict[str, float]
    t_loadings: dict[str, float | None]
    r2: float | None
    n_days: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "alpha_daily": round(self.alpha_daily, 8),
            "alpha_annual": round(self.alpha_annual, 6),
            "t_alpha": None if self.t_alpha is None else round(self.t_alpha, 4),
            "loadings": {k: round(v, 4) for k, v in self.loadings.items()},
            "t_loadings": {k: None if v is None else round(v, 4)
                           for k, v in self.t_loadings.items()},
            "r2": None if self.r2 is None else round(self.r2, 4),
            "n_days": self.n_days,
            "model": "fama_french_3",
            "note": "Alpha here is RESIDUAL of market, size and value exposure — "
                    "not excess return over a single benchmark. Offline "
                    "evaluation only; never a trade signal.",
        }


def align_to_factors(returns_by_day: dict[date, float],
                     rows: list[FactorRow]) -> tuple[list[float], list[list[float]]]:
    """Inner-join a dated return series against the factor series.

    Returns ``(excess_returns, [mkt_rf, smb, hml])`` over the days present in
    BOTH, in ascending date order. Excess is computed per day against that
    day's own ``RF``, which is what the factor construction assumes.
    """
    excess: list[float] = []
    columns: list[list[float]] = [[], [], []]
    for row in rows:  # ascending by construction
        strategy = returns_by_day.get(row.day)
        if strategy is None:
            continue  # a day the strategy did not trade is not a zero-return day
        excess.append(strategy - row.rf)
        columns[0].append(row.mkt_rf)
        columns[1].append(row.smb)
        columns[2].append(row.hml)
    return excess, columns


def attribute(returns_by_day: dict[date, float],
              rows: list[FactorRow], *, min_obs: int = 61) -> FactorAttribution | None:
    """Three-factor attribution of a dated return series.

    ``None`` when fewer than ``min_obs`` days overlap the factor series or the
    regression is unestimable — an unreportable result, never a zeroed one.
    """
    excess, columns = align_to_factors(returns_by_day, rows)
    fit = multi_ols(excess, columns, min_obs=min_obs)
    if fit is None:
        return None
    betas: list[float] = fit["betas"]
    t_betas: list[float | None] = fit["t_betas"]
    return FactorAttribution(
        alpha_daily=fit["alpha_daily"],
        alpha_annual=fit["alpha_annual"],
        t_alpha=fit["t_alpha"],
        loadings=dict(zip(FACTOR_NAMES, betas, strict=True)),
        t_loadings=dict(zip(FACTOR_NAMES, t_betas, strict=True)),
        r2=fit["r2"],
        n_days=fit["n_days"],
    )
