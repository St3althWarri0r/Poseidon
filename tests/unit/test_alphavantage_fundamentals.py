# tests/unit/test_alphavantage_fundamentals.py
"""Alpha Vantage fundamentals + insider extension (r2-wave2 rank 4). No network.

AV serves every numeric field as a STRING with stub literals ('None', '-', '',
'N/A') sprinkled in — these must map to None, never raise InvalidOperation into
the tool path. OVERVIEW {} (unknown symbol) is a non-retryable ProviderError;
an empty INSIDER_TRANSACTIONS data list is a SUCCESS ("none reported"), not an
error. The existing _get rate-limit mapping ('Note' -> ProviderRateLimitError)
must cover the new functions for free.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from poseidon.core.errors import ProviderError, ProviderRateLimitError
from poseidon.data.base import DataCapability
from poseidon.data.providers.alphavantage import AlphaVantageProvider

_OVERVIEW = {
    "Symbol": "AAPL",
    "Name": "Apple Inc",
    "Sector": "TECHNOLOGY",
    "Industry": "ELECTRONIC COMPUTERS",
    "Description": "Apple Inc. designs, manufactures and markets smartphones.",
    "MarketCapitalization": "3400120000000",
    "RevenueTTM": "391035000000",
    "EPS": "6.42",
    "AnalystTargetPrice": "252.5",
    "SharesOutstanding": "15115823000",
    "PERatio": "34.2",
    "ForwardPE": "29.1",
    "PEGRatio": "2.5",
    "PriceToBookRatio": "48.1",
    "EVToEBITDA": "25.4",
    "ProfitMargin": "0.152",
    "OperatingMarginTTM": "0.31",
    "ReturnOnEquityTTM": "1.47",
    "DividendYield": "0.0044",
    "Beta": "1.24",
}

_INCOME = {
    "symbol": "AAPL",
    "annualReports": [
        {"fiscalDateEnding": "2025-09-27", "reportedCurrency": "USD",
         "totalRevenue": "391035000000", "netIncome": "93736000000",
         "grossProfit": "180683000000", "operatingIncome": "123216000000"},
    ],
    "quarterlyReports": [
        {"fiscalDateEnding": "2025-06-28", "reportedCurrency": "USD",
         "totalRevenue": "85777000000", "netIncome": "23434000000",
         "grossProfit": "39678000000", "operatingIncome": "None"},  # stub value
    ],
}

_BALANCE = {
    "symbol": "AAPL",
    "annualReports": [
        {"fiscalDateEnding": "2025-09-27", "reportedCurrency": "USD",
         "totalAssets": "364980000000", "totalLiabilities": "308030000000",
         "totalShareholderEquity": "56950000000",
         "cashAndCashEquivalentsAtCarryingValue": "29943000000",
         "longTermDebtNoncurrent": "85750000000"},
    ],
    "quarterlyReports": [],
}

_CASHFLOW = {
    "symbol": "AAPL",
    "annualReports": [
        {"fiscalDateEnding": "2025-09-27", "reportedCurrency": "USD",
         "operatingCashflow": "118254000000", "capitalExpenditures": "9447000000"},
    ],
    "quarterlyReports": [],
}

_INSIDER = {
    "data": [
        {"transaction_date": "2026-02-26", "ticker": "AAPL",
         "executive": "Cook, Timothy", "executive_title": "Chief Executive Officer",
         "security_type": "Common Stock", "acquisition_or_disposal": "D",
         "shares": "3334", "share_price": "236.95"},
        {"transaction_date": "2026-02-01", "ticker": "AAPL",
         "executive": "Adams, Katherine", "executive_title": "General Counsel",
         "security_type": "Common Stock", "acquisition_or_disposal": "A",
         "shares": "1000", "share_price": "0.0"},  # grant: price 0 -> None
    ],
}


def _provider(payloads: dict[str, Any] | None = None,
              seen: list[httpx.Request] | None = None) -> AlphaVantageProvider:
    table: dict[str, Any] = {
        "OVERVIEW": _OVERVIEW, "INCOME_STATEMENT": _INCOME, "BALANCE_SHEET": _BALANCE,
        "CASH_FLOW": _CASHFLOW, "INSIDER_TRANSACTIONS": _INSIDER,
    }
    table.update(payloads or {})

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        payload = table.get(req.url.params.get("function", ""), {})
        return httpx.Response(200, content=json.dumps(payload).encode(),
                              headers={"content-type": "application/json"})

    provider = AlphaVantageProvider(api_key="k")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


# ----------------------------------------------------------- capabilities


def test_capabilities_include_fundamentals_and_insider() -> None:
    caps = AlphaVantageProvider(api_key="k").capabilities()
    assert DataCapability.FUNDAMENTALS in caps
    assert DataCapability.INSIDER in caps
    # existing capabilities preserved
    assert {DataCapability.QUOTES, DataCapability.NEWS} <= caps
    assert DataCapability.FILINGS not in caps  # AV serves no filing metadata


# ----------------------------------------------------------- fundamentals


async def test_overview_mapping_decimal_and_float_split() -> None:
    report = await _provider().fundamentals("AAPL")
    assert report.symbol == "AAPL" and report.source == "alphavantage"
    ov = report.overview
    assert ov is not None
    assert ov.name == "Apple Inc" and ov.sector == "TECHNOLOGY"
    # money-like: exact Decimals from AV's strings
    assert ov.market_cap == Decimal("3400120000000")
    assert ov.revenue_ttm == Decimal("391035000000")
    assert ov.eps_ttm == Decimal("6.42")
    assert ov.analyst_target == Decimal("252.5")
    assert ov.shares_outstanding == Decimal("15115823000")
    assert isinstance(ov.market_cap, Decimal)
    # dimensionless ratios: floats
    assert ov.pe_ratio == pytest.approx(34.2)
    assert ov.profit_margin == pytest.approx(0.152)
    assert ov.beta == pytest.approx(1.24)


async def test_overview_stub_values_map_to_none_not_invalid_operation() -> None:
    overview = dict(_OVERVIEW)
    overview.update({"MarketCapitalization": "None", "RevenueTTM": "-",
                     "EPS": "", "PERatio": "N/A", "Beta": "None"})
    report = await _provider({"OVERVIEW": overview}).fundamentals("AAPL")
    ov = report.overview
    assert ov is not None
    assert ov.market_cap is None and ov.revenue_ttm is None and ov.eps_ttm is None
    assert ov.pe_ratio is None and ov.beta is None


async def test_statement_periods_curated_canonical_keys() -> None:
    report = await _provider().fundamentals("AAPL")
    by_key = {(p.fiscal_date_ending, p.period): p for p in report.statements}
    annual = by_key[(date(2025, 9, 27), "annual")]
    quarterly = by_key[(date(2025, 6, 28), "quarterly")]

    # income + balance + cash-flow merged into ONE period per fiscal date
    assert annual.items["revenue"] == Decimal("391035000000")
    assert annual.items["net_income"] == Decimal("93736000000")
    assert annual.items["gross_profit"] == Decimal("180683000000")
    assert annual.items["total_assets"] == Decimal("364980000000")
    assert annual.items["shareholder_equity"] == Decimal("56950000000")
    assert annual.items["long_term_debt"] == Decimal("85750000000")
    assert annual.items["operating_cashflow"] == Decimal("118254000000")
    assert annual.items["capital_expenditures"] == Decimal("9447000000")
    assert annual.currency == "USD"
    # AV does not publish a filed date — honesty over invention
    assert annual.filed is None and annual.form is None

    assert quarterly.items["revenue"] == Decimal("85777000000")
    assert "operating_income" not in quarterly.items  # stub 'None' omitted


async def test_statements_newest_first_and_bounded() -> None:
    income = {
        "symbol": "AAPL",
        "annualReports": [
            {"fiscalDateEnding": f"20{y}-09-27", "reportedCurrency": "USD",
             "totalRevenue": "1"} for y in range(25, 13, -1)  # 12 annuals
        ],
        "quarterlyReports": [],
    }
    report = await _provider({"OVERVIEW": _OVERVIEW, "INCOME_STATEMENT": income,
                              "BALANCE_SHEET": {}, "CASH_FLOW": {}}).fundamentals("AAPL")
    assert len(report.statements) == 8  # bounded to ~8 periods
    ends = [p.fiscal_date_ending for p in report.statements]
    assert ends == sorted(ends, reverse=True)


async def test_empty_overview_raises_non_retryable() -> None:
    provider = _provider({"OVERVIEW": {}})
    with pytest.raises(ProviderError, match="no fundamentals for MISSING") as exc_info:
        await provider.fundamentals("MISSING")
    assert exc_info.value.retryable is False


async def test_rate_limit_note_maps_to_rate_limit_error() -> None:
    provider = _provider({"OVERVIEW": {"Note": "API call frequency exceeded"}})
    with pytest.raises(ProviderRateLimitError):
        await provider.fundamentals("AAPL")


# ----------------------------------------------------------- insider


async def test_insider_rows_mapped_signed_shares_and_price() -> None:
    rows = await _provider().insider_transactions("AAPL")
    assert len(rows) == 2
    sale, grant = rows
    assert sale.symbol == "AAPL" and sale.source == "alphavantage"
    assert sale.name == "Cook, Timothy"
    assert sale.title == "Chief Executive Officer"
    assert sale.transaction_date == date(2026, 2, 26)
    assert sale.shares_changed == Decimal("-3334")  # D = disposal -> negative
    assert sale.price == Decimal("236.95")
    assert grant.shares_changed == Decimal("1000")  # A = acquisition -> positive
    assert grant.price is None                      # 0-price grant is not a market price


async def test_insider_empty_data_is_success_not_error() -> None:
    rows = await _provider({"INSIDER_TRANSACTIONS": {"data": []}}).insider_transactions("AAPL")
    assert rows == []


async def test_insider_bounded_by_limit() -> None:
    rows = await _provider().insider_transactions("AAPL", limit=1)
    assert len(rows) == 1
