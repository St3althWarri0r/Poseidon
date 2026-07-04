"""ETF / sector rotation: rank the configured universe by blended momentum."""

from __future__ import annotations

from ...data.router import DataRouter
from ...portfolio.state import PortfolioState
from ..base import Signal, Strategy, gather_bars, pct_return

_DEFAULT_UNIVERSE = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


class EtfRotationStrategy(Strategy):
    name = "etf_rotation"
    description = "Rank a sector-ETF universe by blended 1/3/6-month momentum; surface leaders and laggard holdings."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        universe = [s.upper() for s in (self.symbols or _DEFAULT_UNIVERSE)]
        top_n = int(self.options.get("top_n", 3))
        scores: list[tuple[str, float, dict[str, float | None]]] = []
        bars_by_symbol = await gather_bars(router, universe, limit=140)
        for symbol, bars in bars_by_symbol.items():
            closes = [float(b.close) for b in bars]
            r21, r63, r126 = pct_return(closes, 21), pct_return(closes, 63), pct_return(closes, 126)
            if r21 is None or r63 is None:
                continue
            blended = 0.5 * r21 + 0.3 * r63 + 0.2 * (r126 or 0.0)
            scores.append((symbol, blended, {"r_1m": r21, "r_3m": r63, "r_6m": r126}))
        if not scores:
            return []
        scores.sort(key=lambda x: x[1], reverse=True)
        leaders = scores[:top_n]
        laggards = {s for s, _, _ in scores[top_n:]}
        signals = [
            Signal(
                strategy=self.name, symbol=symbol, direction="long",
                strength=min(max(score * 5, 0.0), 1.0),
                evidence={"rank": i + 1, "blended_momentum": round(score, 4),
                          **{k: round(v, 4) for k, v in parts.items() if v is not None}},
            )
            for i, (symbol, score, parts) in enumerate(leaders) if score > 0
        ]
        # Flag held laggards for rotation out.
        for position in portfolio.positions:
            if position.symbol.upper() in laggards:
                signals.append(
                    Signal(
                        strategy=self.name, symbol=position.symbol, direction="exit",
                        strength=0.6,
                        evidence={"note": "held ETF ranks below rotation cut", "top_n": top_n},
                    )
                )
        return signals
