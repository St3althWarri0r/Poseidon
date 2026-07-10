"""Opt-in live Yahoo smoke: POSEIDON_LIVE_TESTS=1 pytest tests/integration/test_terminal_live.py"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSEIDON_LIVE_TESTS"),
    reason="live Yahoo test; set POSEIDON_LIVE_TESTS=1 to run",
)


async def test_live_quote_and_chart() -> None:
    from poseidon.terminal import yahoo

    quotes = await yahoo.get_quotes(["AAPL"])
    assert quotes and quotes[0]["symbol"] == "AAPL" and quotes[0]["price"] is not None

    chart = await yahoo.get_chart("AAPL", "1M")
    assert len(chart["candles"]) > 5
    times = [c["time"] for c in chart["candles"]]
    assert times == sorted(set(times))  # strictly ascending, deduped
