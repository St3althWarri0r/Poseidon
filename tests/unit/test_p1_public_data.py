"""Regression pins for public_data.py sweep findings (no network).

Each test drives PublicDataProvider through its own fetch seam (``_get_json``,
the only external call bars() makes — see the sibling test
``test_public.py::test_bars_parse_and_timeframe``) with a canned payload, so the
row-parse loop under test runs unchanged. The access token is preset so
``_headers()`` never reaches the network.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from poseidon.data.providers.public_data import PublicDataProvider


def make_provider() -> PublicDataProvider:
    provider = PublicDataProvider(
        api_key="s3cret", options={"account_id": "ACC1", "crypto_symbols": ["BTC"]}
    )
    provider._access_token = "tok"
    provider._token_expiry = 1e12  # never refreshes (no network) inside a test
    return provider


# F012 — a bar row with an explicit ``volume: null`` must not abort bars().
# Pre-fix, ``int(float(row.get("volume", 0)))`` evaluated float(None) (the .get
# default only applies to a MISSING key, not a present null), raising TypeError
# that escaped the per-row ``except (KeyError, ValueError)`` and killed the whole
# request. The fix coerces the null to 0 (``... or 0``) and widens the except.
async def test_f012_null_volume_row_does_not_abort_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_provider()

    async def fake_get(url: str, *, params: Any = None, headers: Any = None) -> Any:
        assert "/historicdata/EQUITY/AAPL/YEAR/ONE_DAY" in url
        return {"regularMarket": {"bars": [
            # good row
            {"timestamp": "2026-07-01T20:00:00Z", "open": "188", "high": "191",
             "low": "187", "close": "190", "volume": "52000000"},
            # bad row: volume is explicitly null (JSON null -> Python None).
            # Pre-fix this raised TypeError from float(None) and aborted bars().
            {"timestamp": "2026-07-02T20:00:00Z", "open": "194", "high": "197",
             "low": "193", "close": "195", "volume": None},
            # good row after the bad one — proves iteration continues.
            {"timestamp": "2026-07-03T20:00:00Z", "open": "190", "high": "192",
             "low": "189", "close": "191", "volume": "48000000"},
        ]}}

    monkeypatch.setattr(provider, "_get_json", fake_get)

    # Pre-fix: this await itself raises TypeError (the null-volume row's exception
    # escapes the row handler). Post-fix it returns every bar.
    bars = await provider.bars("AAPL", timeframe="1d", limit=10)

    # The null-volume row did not sink the request: all three bars survive, in
    # order, with the good rows intact.
    assert [b.close for b in bars] == [Decimal("190"), Decimal("195"), Decimal("191")]
    assert bars[0].volume == 52000000
    assert bars[2].volume == 48000000
    # The explicit null was coerced to 0 by the ``... or 0`` guard (the fixed
    # line) rather than raising or being dropped.
    assert bars[1].volume == 0
