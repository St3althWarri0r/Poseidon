"""Strategy interface.

Strategies are quantitative *screeners*, not order generators. Each enabled
strategy inspects live data for its symbols and emits Signal candidates
("AAPL momentum long: 20d return +8.4%, above 50d MA, volume 1.6x avg").
Signals flow into the AI review cycle as structured context; Claude weighs
them against portfolio state, news, and event risk, and only Claude's
decision — after the risk engine — can become an order.

This split keeps the math testable and deterministic while the judgment
layer stays with the AI, and it means a strategy bug can never place an
order by itself.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ..data.router import DataRouter
from ..portfolio.state import PortfolioState


@dataclass
class Signal:
    strategy: str
    symbol: str
    direction: str  # "long" | "short" | "exit" | "hedge" | "income"
    strength: float  # 0..1, strategy-specific scoring
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy, "symbol": self.symbol,
            "direction": self.direction, "strength": round(self.strength, 3),
            "evidence": self.evidence,
        }


class Strategy(abc.ABC):
    """Base class for strategies. ``name`` matches config ``strategies[].name``."""

    name: str = ""
    description: str = ""

    def __init__(self, *, symbols: list[str], options: dict[str, Any] | None = None) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.options = options or {}

    @abc.abstractmethod
    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        """Inspect live data and return zero or more signals. Data failures
        for one symbol must not abort the scan for the rest."""


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def pct_return(closes: list[float], periods: int) -> float | None:
    if len(closes) <= periods or closes[-periods - 1] == 0:
        return None
    return closes[-1] / closes[-periods - 1] - 1.0


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    """Annualized close-to-close volatility."""
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - window, len(closes))
            if closes[i - 1] != 0]
    if not rets:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return (var ** 0.5) * (252 ** 0.5)
