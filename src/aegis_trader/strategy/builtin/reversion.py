"""Mean-reversion strategies: single-name z-score reversion and pairs."""

from __future__ import annotations

from ...core.errors import DataError
from ...data.router import DataRouter
from ...portfolio.state import PortfolioState
from ..base import Signal, Strategy, sma


def _zscore(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window:
        return None
    sample = closes[-window:]
    mean = sum(sample) / window
    var = sum((c - mean) ** 2 for c in sample) / window
    std = float(var ** 0.5)
    if std == 0:
        return None
    return (closes[-1] - mean) / std


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    description = "Oversold names (z-score < -2 vs 20-day mean) inside a longer uptrend."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        entry_z = float(self.options.get("entry_zscore", -2.0))
        for symbol in self.symbols:
            try:
                bars = await router.bars(symbol, timeframe="1d", limit=120)
            except DataError:
                continue
            closes = [float(b.close) for b in bars]
            z = _zscore(closes)
            ma100 = sma(closes, 100)
            if z is None or ma100 is None:
                continue
            if z <= entry_z and closes[-1] > ma100:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="long",
                        strength=min(abs(z) / 4.0, 1.0),
                        evidence={"zscore_20d": round(z, 2), "close": closes[-1],
                                  "ma100": round(ma100, 2)},
                    )
                )
            elif z >= abs(entry_z) and portfolio.position_for(symbol) is not None:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="exit",
                        strength=min(z / 4.0, 1.0),
                        evidence={"zscore_20d": round(z, 2), "note": "stretched above mean; review exit"},
                    )
                )
        return signals


class PairsStrategy(Strategy):
    name = "pairs"
    description = "Spread divergence between configured pairs (options.pairs: [[A,B],...])."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        pairs: list[list[str]] = self.options.get("pairs", [])
        threshold = float(self.options.get("entry_zscore", 2.0))
        for pair in pairs:
            if len(pair) != 2:
                continue
            a, b = pair[0].upper(), pair[1].upper()
            try:
                bars_a = await router.bars(a, timeframe="1d", limit=90)
                bars_b = await router.bars(b, timeframe="1d", limit=90)
            except DataError:
                continue
            closes_a = [float(x.close) for x in bars_a]
            closes_b = [float(x.close) for x in bars_b]
            n = min(len(closes_a), len(closes_b))
            if n < 40:
                continue
            ratio = [closes_a[-n + i] / closes_b[-n + i] for i in range(n) if closes_b[-n + i] != 0]
            z = _zscore(ratio, window=30)
            if z is None:
                continue
            if abs(z) >= threshold:
                rich, cheap = (a, b) if z > 0 else (b, a)
                signals.append(
                    Signal(
                        strategy=self.name, symbol=cheap, direction="long",
                        strength=min(abs(z) / 4.0, 1.0),
                        evidence={"pair": f"{a}/{b}", "ratio_zscore_30d": round(z, 2),
                                  "rich_leg": rich, "cheap_leg": cheap},
                    )
                )
        return signals
