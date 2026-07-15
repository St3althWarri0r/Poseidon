# tests/unit/test_research_loader.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.loader import load_history


class _Router:
    async def bars(self, symbol, *, timeframe="1d", limit=100):
        if symbol == "BAD":
            raise RuntimeError("no data")
        d = datetime(2024, 1, 1, tzinfo=UTC)
        return [Bar(symbol=symbol, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
                    close=Decimal("1"), volume=1, start=d, end=d, source="t")]


async def test_load_history_skips_failures() -> None:
    hist = await load_history(_Router(), ["AAA", "BAD", "BBB"], days=30)
    assert set(hist) == {"AAA", "BBB"}              # BAD skipped, no raise
