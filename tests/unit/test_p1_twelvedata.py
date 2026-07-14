"""Regression pins for the Twelve Data provider (F013). No network.

Drives ``TwelveDataProvider.bars()`` over an ``httpx.MockTransport`` so the real
``_get``/``_get_json``/``_decode`` + JSON path (and the exact per-row parse line)
runs, exactly like the broker plugin tests in ``test_brokers.py``.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from poseidon.data.providers.twelvedata import TwelveDataProvider


def _provider_returning(payload: dict[str, object]) -> TwelveDataProvider:
    """A TwelveDataProvider whose HTTP client is a MockTransport that always
    replies with ``payload`` as a Twelve Data JSON body (HTTP 200, the way the
    upstream API reports even in-band errors)."""
    body = json.dumps(payload).encode()
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    provider = TwelveDataProvider(api_key="test")
    provider._client = httpx.AsyncClient(transport=transport)
    return provider


# F013: Twelve Data returns OHLCV as JSON strings and can emit "" for a missing
# field; Decimal("") raises decimal.InvalidOperation (an ArithmeticError, NOT a
# ValueError). Pre-fix the per-row `except (KeyError, ValueError)` did not catch
# it, so one malformed row raised out of bars() (a non-PoseidonError that also
# defeated router failover) instead of being skipped. The fix adds
# InvalidOperation/TypeError to the row except so a single bad row is dropped and
# the good rows are still returned. This test fails pre-fix (bars() raises
# InvalidOperation) and passes post-fix.
async def test_f013_empty_ohlc_string_row_skipped_not_fatal() -> None:
    payload = {
        "status": "ok",
        "values": [  # Twelve Data returns newest-first
            {"datetime": "2026-07-03", "open": "190", "high": "192",
             "low": "189", "close": "191", "volume": "48000000"},
            # malformed row: an empty OHLC field -> Decimal("") -> InvalidOperation
            {"datetime": "2026-07-02", "open": "", "high": "192",
             "low": "189", "close": "777", "volume": "50000000"},
            {"datetime": "2026-07-01", "open": "188", "high": "191",
             "low": "187", "close": "189", "volume": "52000000"},
        ],
    }
    provider = _provider_returning(payload)

    bars = await provider.bars("aapl", timeframe="1d", limit=10)

    # The one malformed row is skipped, not fatal: the two good rows survive
    # (bars() reverses to oldest-first), and the bad row's sentinel never appears.
    assert len(bars) == 2
    assert [b.close for b in bars] == [Decimal("189"), Decimal("191")]
    assert Decimal("777") not in [b.close for b in bars]
    assert all(b.symbol == "AAPL" for b in bars)
