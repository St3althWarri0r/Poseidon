# tests/unit/test_analysis_snapshot.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.ai.analysis.snapshot import Snapshot, build_snapshot


class _FakeQuote:
    # Prices are Decimal end-to-end in Poseidon; the snapshot must render them
    # EXACTLY (no lossy rounding) — str(Decimal("190.10")) preserves the cents.
    price = Decimal("190.10")
    as_of = datetime.now(UTC)
    source = "fake"


class _FakeRouter:
    async def quote(self, symbol, allow_delayed=True):
        return _FakeQuote()
    async def bars(self, symbol, timeframe="1d", limit=50):
        return []


async def test_snapshot_pins_price() -> None:
    snap = await build_snapshot(_FakeRouter(), "AAPL")
    assert isinstance(snap, Snapshot)
    assert "190.10" in snap.text and "AAPL" in snap.text
    assert snap.source == "fake"


async def test_snapshot_none_on_failure() -> None:
    class _Dead:
        async def quote(self, *a, **k):
            raise RuntimeError("no data")
        async def bars(self, *a, **k):
            return []
    assert await build_snapshot(_Dead(), "AAPL") is None
