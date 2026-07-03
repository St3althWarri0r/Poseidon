"""Trend-following strategies: momentum, breakout, swing."""

from __future__ import annotations

import structlog

from ...core.errors import DataError
from ...data.router import DataRouter
from ...portfolio.state import PortfolioState
from ..base import Signal, Strategy, pct_return, sma

log = structlog.get_logger(__name__)


class MomentumStrategy(Strategy):
    name = "momentum"
    description = "Long candidates with strong 20/60-day returns above the 50-day average."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        min_return = float(self.options.get("min_20d_return", 0.05))
        for symbol in self.symbols:
            try:
                bars = await router.bars(symbol, timeframe="1d", limit=90)
            except DataError as exc:
                log.debug("momentum scan skip", symbol=symbol, error=str(exc))
                continue
            closes = [float(b.close) for b in bars]
            r20 = pct_return(closes, 20)
            r60 = pct_return(closes, 60)
            ma50 = sma(closes, 50)
            if r20 is None or ma50 is None:
                continue
            vols = [b.volume for b in bars]
            vol_ratio = (vols[-1] / (sum(vols[-21:-1]) / 20)) if len(vols) >= 21 and sum(vols[-21:-1]) else None
            if r20 >= min_return and closes[-1] > ma50 and (r60 is None or r60 > 0):
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="long",
                        strength=min(r20 / (min_return * 4), 1.0),
                        evidence={
                            "return_20d": round(r20, 4),
                            "return_60d": round(r60, 4) if r60 is not None else None,
                            "close": closes[-1], "ma50": round(ma50, 2),
                            "volume_vs_20d_avg": round(vol_ratio, 2) if vol_ratio else None,
                        },
                    )
                )
        return signals


class BreakoutStrategy(Strategy):
    name = "breakouts"
    description = "New 55-day highs on above-average volume."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        lookback = int(self.options.get("lookback_days", 55))
        vol_multiple = float(self.options.get("min_volume_multiple", 1.5))
        for symbol in self.symbols:
            try:
                bars = await router.bars(symbol, timeframe="1d", limit=lookback + 10)
            except DataError:
                continue
            if len(bars) < lookback + 1:
                continue
            closes = [float(b.close) for b in bars]
            prior_high = max(closes[-lookback - 1:-1])
            avg_vol = sum(b.volume for b in bars[-21:-1]) / 20 if len(bars) >= 21 else None
            breakout = closes[-1] > prior_high
            volume_ok = avg_vol is not None and avg_vol > 0 and bars[-1].volume >= avg_vol * vol_multiple
            if breakout and volume_ok:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="long",
                        strength=min((closes[-1] / prior_high - 1) * 20, 1.0),
                        evidence={
                            "close": closes[-1], "prior_high": round(prior_high, 2),
                            "lookback_days": lookback,
                            "volume_multiple": round(bars[-1].volume / avg_vol, 2),
                        },
                    )
                )
        return signals


class SwingStrategy(Strategy):
    name = "swing"
    description = "Pullbacks to the 20-day average inside an uptrend (swing entries)."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        for symbol in self.symbols:
            try:
                bars = await router.bars(symbol, timeframe="1d", limit=80)
            except DataError:
                continue
            closes = [float(b.close) for b in bars]
            ma20, ma50 = sma(closes, 20), sma(closes, 50)
            if ma20 is None or ma50 is None:
                continue
            uptrend = ma20 > ma50 and closes[-1] > ma50
            near_ma20 = abs(closes[-1] - ma20) / ma20 <= float(self.options.get("ma_band", 0.02))
            if uptrend and near_ma20:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="long",
                        strength=0.5 + min((ma20 / ma50 - 1) * 10, 0.5),
                        evidence={"close": closes[-1], "ma20": round(ma20, 2), "ma50": round(ma50, 2)},
                    )
                )
        return signals
