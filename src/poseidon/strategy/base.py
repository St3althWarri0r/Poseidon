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
import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import DataError
from ..core.models import Bar
from ..data.router import DataRouter
from ..portfolio.state import PortfolioState
from . import indicators


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
    async def scan(self, router: DataRouter, portfolio: PortfolioState, *,
                   extra_symbols: list[str] | None = None) -> list[Signal]:
        """Inspect live data and return zero or more signals. Data failures
        for one symbol must not abort the scan for the rest.

        ``extra_symbols`` are screener-supplied candidates the strategy should
        additionally consider this cycle (default ``None`` ⇒ unchanged, only
        the configured universe). It is advisory *selection* only — it widens
        WHAT gets screened; every emitted signal still flows through the AI and
        the RiskEngine unchanged. Equity screeners fold it into their universe
        via :meth:`_widen`; options strategies accept it but ignore it (selling
        options on unheld screened names is out of scope)."""

    def _widen(self, extra_symbols: list[str] | None,
               *, base: list[str] | None = None) -> list[str]:
        """The strategy's configured universe (or ``base`` when it resolves its
        own default) plus any screener-supplied ``extra_symbols`` — uppercased,
        order-stable, de-duplicated (configured names first). ``extra_symbols``
        is ``None`` ⇒ returns the base universe unchanged."""
        source = self.symbols if base is None else base
        out: list[str] = []
        seen: set[str] = set()
        for sym in (*source, *(s.upper() for s in (extra_symbols or []))):
            upper = sym.upper()
            if upper not in seen:
                seen.add(upper)
                out.append(upper)
        return out


async def gather_bars(router: DataRouter, symbols: list[str], *,
                      timeframe: str = "1d", limit: int = 120) -> dict[str, list[Bar]]:
    """Fetch daily bars for many symbols concurrently.

    Returns ``{symbol: bars}`` in the input order. A per-symbol data failure
    yields an empty list for that symbol so one bad ticker never aborts the
    batch — the caller's None-guards then skip it, exactly as the old
    per-symbol try/except did, but the whole universe is fetched in one
    round-trip's latency instead of N sequential ones.
    """
    async def _one(sym: str) -> list[Bar]:
        try:
            return await router.bars(sym, timeframe=timeframe, limit=limit)
        except DataError:
            return []

    results = await asyncio.gather(*(_one(s) for s in symbols))
    return dict(zip(symbols, results, strict=True))


def sma(values: list[float], window: int) -> float | None:
    # Single source of truth in indicators.py (also guards window <= 0).
    return indicators.sma(values, window)


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
    return float((var ** 0.5) * (252 ** 0.5))
