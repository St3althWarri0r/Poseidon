# tests/unit/test_finnhub_insider.py
"""Finnhub insider-transactions extension (r2-wave2 rank 4). No network.

/stock/insider-transactions rows map to InsiderTransaction: ``change`` is the
signed share delta (verbatim Decimal), ``transactionPrice`` 0 means no market
price (None), and an empty ``data`` list is a SUCCESS ("none reported")."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from poseidon.data.base import DataCapability
from poseidon.data.providers.finnhub import FinnhubProvider

_ROWS = {
    "data": [
        {"name": "Cook Timothy", "share": 3300000, "change": -3334,
         "filingDate": "2026-03-01", "transactionDate": "2026-02-26",
         "transactionCode": "S", "transactionPrice": 236.95},
        {"name": "Adams Katherine", "share": 200000, "change": 1000,
         "filingDate": "2026-02-03", "transactionDate": "2026-02-01",
         "transactionCode": "A", "transactionPrice": 0},  # grant: 0 -> None
    ],
    "symbol": "AAPL",
}


def _provider(payload: Any = None, seen: list[httpx.Request] | None = None) -> FinnhubProvider:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        body = _ROWS if payload is None else payload
        return httpx.Response(200, content=json.dumps(body).encode(),
                              headers={"content-type": "application/json"})

    provider = FinnhubProvider(api_key="k")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def test_capabilities_include_insider() -> None:
    caps = FinnhubProvider(api_key="k").capabilities()
    assert DataCapability.INSIDER in caps
    # existing capabilities preserved
    assert {DataCapability.QUOTES, DataCapability.NEWS, DataCapability.EARNINGS,
            DataCapability.ECONOMIC_CALENDAR, DataCapability.SECTOR,
            DataCapability.PROFILE} <= caps
    assert DataCapability.FUNDAMENTALS not in caps  # not served on this seam


async def test_insider_rows_mapped() -> None:
    seen: list[httpx.Request] = []
    rows = await _provider(seen=seen).insider_transactions("AAPL")

    assert seen[0].url.path == "/api/v1/stock/insider-transactions"
    assert seen[0].url.params.get("symbol") == "AAPL"

    assert len(rows) == 2
    sale, grant = rows
    assert sale.symbol == "AAPL" and sale.source == "finnhub"
    assert sale.name == "Cook Timothy"
    assert sale.shares_changed == Decimal("-3334")  # signed, verbatim
    assert sale.price == Decimal("236.95")
    assert sale.code == "S"
    assert sale.transaction_date == date(2026, 2, 26)
    assert sale.filing_date == date(2026, 3, 1)

    assert grant.shares_changed == Decimal("1000")
    assert grant.price is None  # transactionPrice 0 -> no market price


async def test_insider_empty_data_is_success() -> None:
    assert await _provider({"data": [], "symbol": "AAPL"}).insider_transactions("AAPL") == []
    assert await _provider({"symbol": "AAPL"}).insider_transactions("AAPL") == []


async def test_insider_bounded_by_limit() -> None:
    rows = await _provider().insider_transactions("AAPL", limit=1)
    assert len(rows) == 1 and rows[0].name == "Cook Timothy"
