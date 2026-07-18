"""Volatility regime strategy: realized-vol expansion/contraction context."""

from __future__ import annotations

from ...data.router import DataRouter
from ...portfolio.state import PortfolioState
from ..base import Signal, Strategy, gather_bars, realized_vol


class VolatilityRegimeStrategy(Strategy):
    name = "volatility"
    description = ("Track 10d vs 60d realized volatility; flag expansion (de-risk/hedge) "
                   "and contraction (premium-selling friendly) regimes.")

    async def scan(self, router: DataRouter, portfolio: PortfolioState, *,
                   extra_symbols: list[str] | None = None) -> list[Signal]:
        signals: list[Signal] = []
        expansion_ratio = float(self.options.get("expansion_ratio", 1.5))
        symbols = self._widen(extra_symbols, base=self.symbols or ["SPY"])
        bars_by_symbol = await gather_bars(router, symbols, limit=90)
        for symbol, bars in bars_by_symbol.items():
            closes = [float(b.close) for b in bars]
            short_vol = realized_vol(closes, 10)
            long_vol = realized_vol(closes, 60)
            if short_vol is None or long_vol is None or long_vol == 0:
                continue
            ratio = short_vol / long_vol
            if ratio >= expansion_ratio:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="hedge",
                        strength=min((ratio - 1) / 2, 1.0),
                        evidence={"regime": "expansion", "rv_10d": round(short_vol, 3),
                                  "rv_60d": round(long_vol, 3), "ratio": round(ratio, 2),
                                  "note": "volatility expanding; review exposure and hedges"},
                    )
                )
            elif ratio <= 1 / expansion_ratio:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="income",
                        strength=min((1 - ratio), 1.0),
                        evidence={"regime": "contraction", "rv_10d": round(short_vol, 3),
                                  "rv_60d": round(long_vol, 3), "ratio": round(ratio, 2),
                                  "note": "quiet regime; premium selling conditions"},
                    )
                )
        return signals
