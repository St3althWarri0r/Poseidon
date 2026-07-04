"""Long-horizon watch strategies: long-term investing, dividends, growth.

These emit low-frequency accumulation/review context rather than trade
triggers: cost-average opportunities on meaningful dips in quality names
the user has designated, plus periodic position reviews.
"""

from __future__ import annotations

from ...data.router import DataRouter
from ...portfolio.state import PortfolioState
from ..base import Signal, Strategy, gather_bars, pct_return, sma


class _WatchBase(Strategy):
    direction = "long"
    dip_key = "dip_pct"
    default_dip = 0.10

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        dip = float(self.options.get(self.dip_key, self.default_dip))
        bars_by_symbol = await gather_bars(router, self.symbols, limit=260)
        for symbol, bars in bars_by_symbol.items():
            closes = [float(b.close) for b in bars]
            if len(closes) < 60:
                continue
            high_52w = max(closes)
            off_high = 1 - closes[-1] / high_52w if high_52w else 0.0
            ma200 = sma(closes, 200)
            r252 = pct_return(closes, min(len(closes) - 1, 252))
            if off_high >= dip:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction=self.direction,
                        strength=min(off_high / (dip * 3), 1.0),
                        evidence={
                            "off_52w_high_pct": round(off_high, 3),
                            "close": closes[-1],
                            "ma200": round(ma200, 2) if ma200 else None,
                            "return_1y": round(r252, 3) if r252 is not None else None,
                            "note": f"designated {self.name} name trading {off_high:.0%} off its high — "
                                    "candidate for scheduled accumulation review",
                        },
                    )
                )
        return signals


class LongTermWatchStrategy(_WatchBase):
    name = "long_term"
    description = "Accumulate designated core holdings on 10%+ pullbacks."


class DividendWatchStrategy(_WatchBase):
    name = "dividend"
    description = "Accumulate designated dividend payers on pullbacks (yield improves as price dips)."
    default_dip = 0.08


class GrowthWatchStrategy(_WatchBase):
    name = "growth"
    description = "Accumulate designated growth names on deeper (15%+) drawdowns."
    default_dip = 0.15
