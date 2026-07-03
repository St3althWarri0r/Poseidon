"""Market regime detection from live benchmark history.

A compact, assumption-light read of the tape that professional desks keep
on one screen: trend (price vs. moving averages), volatility state
(realized vol vs. its own trailing distribution), and drawdown from the
one-year high, folded into a four-state classification:

  * ``risk_on``  — uptrend, unexceptional volatility
  * ``neutral``  — mixed evidence
  * ``risk_off`` — downtrend and/or elevated volatility
  * ``stress``   — volatility extreme or deep index drawdown

The regime feeds the AI's cycle context (sizing and posture, not a
signal) and the dashboard. It is computed from live bars only; with
insufficient history the state is ``unknown`` — never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

TRADING_DAYS = 252
_VOL_WINDOW = 20
_MIN_BARS = 60


@dataclass
class RegimeReport:
    state: str  # risk_on | neutral | risk_off | stress | unknown
    as_of: datetime
    benchmark: str
    trend: str  # uptrend | downtrend | sideways | unknown
    close: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    realized_vol_annualized: float | None = None
    vol_percentile: float | None = None  # vs. trailing 1y of 20d vol readings
    drawdown_from_high: float | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "as_of": self.as_of.isoformat(),
            "benchmark": self.benchmark,
            "trend": self.trend,
            "close": round(self.close, 2) if self.close is not None else None,
            "sma_50": round(self.sma_50, 2) if self.sma_50 is not None else None,
            "sma_200": round(self.sma_200, 2) if self.sma_200 is not None else None,
            "realized_vol_annualized": (
                round(self.realized_vol_annualized, 4)
                if self.realized_vol_annualized is not None else None
            ),
            "vol_percentile": round(self.vol_percentile, 2) if self.vol_percentile is not None else None,
            "drawdown_from_high": (
                round(self.drawdown_from_high, 4)
                if self.drawdown_from_high is not None else None
            ),
            "detail": self.detail,
        }

    def summary_line(self) -> str:
        """One line for the AI cycle prompt."""
        if self.state == "unknown":
            return f"unknown ({self.detail})"
        bits = [f"{self.state.upper()} — {self.benchmark} {self.trend}"]
        if self.vol_percentile is not None:
            bits.append(f"20d vol at the {self.vol_percentile:.0%} percentile of its 1y range")
        if self.drawdown_from_high is not None and self.drawdown_from_high > 0.02:
            bits.append(f"{self.drawdown_from_high:.1%} below its 1y high")
        return ", ".join(bits)


def _sma(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _rolling_vols(closes: list[float], window: int = _VOL_WINDOW) -> list[float]:
    """Trailing series of annualized close-to-close vol readings."""
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    vols: list[float] = []
    for end in range(window, len(rets) + 1):
        sample = rets[end - window:end]
        mean = sum(sample) / window
        var = sum((r - mean) ** 2 for r in sample) / window
        vols.append(float((var ** 0.5) * (TRADING_DAYS ** 0.5)))
    return vols


def compute_regime(closes: list[float], *, benchmark: str) -> RegimeReport:
    now = datetime.now(UTC)
    if len(closes) < _MIN_BARS:
        return RegimeReport(
            state="unknown", as_of=now, benchmark=benchmark, trend="unknown",
            detail=f"only {len(closes)} daily closes available (need {_MIN_BARS})",
        )
    close = closes[-1]
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)
    year = closes[-TRADING_DAYS:]
    drawdown = max(0.0, (max(year) - close) / max(year)) if max(year) > 0 else 0.0

    vols = _rolling_vols(closes)
    current_vol = vols[-1] if vols else None
    vol_pct: float | None = None
    if current_vol is not None and len(vols) >= 30:
        trailing = vols[-TRADING_DAYS:]
        # Midrank percentile: ties count half, so a flat vol history reads
        # as the 50th percentile, not a spurious extreme.
        below = sum(1 for v in trailing if v < current_vol)
        ties = sum(1 for v in trailing if v == current_vol)
        vol_pct = (below + 0.5 * ties) / len(trailing)

    if sma_50 is not None and close > sma_50 and (sma_200 is None or sma_50 > sma_200):
        trend = "uptrend"
    elif sma_50 is not None and close < sma_50 and (sma_200 is None or close < sma_200):
        trend = "downtrend"
    else:
        trend = "sideways"

    high_vol = vol_pct is not None and vol_pct >= 0.70
    extreme_vol = vol_pct is not None and vol_pct >= 0.90
    if extreme_vol or drawdown >= 0.15:
        state = "stress"
    elif trend == "downtrend" or high_vol or drawdown >= 0.08:
        state = "risk_off"
    elif trend == "uptrend" and not high_vol:
        state = "risk_on"
    else:
        state = "neutral"

    detail_bits = [f"trend={trend}"]
    if vol_pct is not None:
        detail_bits.append(f"vol_pctile={vol_pct:.0%}")
    detail_bits.append(f"drawdown={drawdown:.1%}")
    return RegimeReport(
        state=state, as_of=now, benchmark=benchmark, trend=trend,
        close=close, sma_50=sma_50, sma_200=sma_200,
        realized_vol_annualized=current_vol, vol_percentile=vol_pct,
        drawdown_from_high=drawdown, detail=", ".join(detail_bits),
    )
