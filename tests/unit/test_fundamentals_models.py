# tests/unit/test_fundamentals_models.py
"""Fundamentals/filings/insider domain models (r2-wave2 rank 4).

Pins the Decimal-exactness contract (model_dump(mode="json") serializes every
money-like field to its verbatim string), the symbol upper-validators, and the
``as_known_at`` point-in-time slice: a statement period is knowable at its
``filed`` date, or ``fiscal_date_ending`` + 90 days when the source did not
publish a filed date — the lookahead guard the factor-research panel builds on.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from poseidon.core.models import (
    Filing,
    FundamentalsOverview,
    FundamentalsReport,
    InsiderTransaction,
    StatementPeriod,
)

_AS_OF = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _period(end: date, *, filed: date | None = None, period: str = "annual",
            form: str | None = "10-K", **items: str) -> StatementPeriod:
    return StatementPeriod(period=period, fiscal_date_ending=end, filed=filed, form=form,
                           currency="USD",
                           items={k: Decimal(v) for k, v in items.items()})


def _report(statements: list[StatementPeriod] | None = None) -> FundamentalsReport:
    return FundamentalsReport(
        symbol="AAPL",
        overview=FundamentalsOverview(
            name="Apple Inc.", sector="Technology",
            market_cap=Decimal("3400000000000"), revenue_ttm=Decimal("391035000000"),
            eps_ttm=Decimal("6.42"), profit_margin=0.152,
        ),
        statements=statements if statements is not None else [
            _period(date(2025, 9, 27), filed=date(2025, 11, 1),
                    revenue="391035000000", net_income="93736000000"),
        ],
        as_of=_AS_OF, source="sec_edgar",
    )


# ----------------------------------------------------------- Decimal exactness


def test_report_decimal_fields_survive_json_dump_verbatim() -> None:
    payload = _report().model_dump(mode="json")
    # pydantic v2 serializes Decimal -> str: exact digits, never a float round.
    assert payload["overview"]["revenue_ttm"] == "391035000000"
    assert payload["overview"]["market_cap"] == "3400000000000"
    assert payload["overview"]["eps_ttm"] == "6.42"
    assert payload["statements"][0]["items"]["revenue"] == "391035000000"
    assert payload["statements"][0]["items"]["net_income"] == "93736000000"
    # dimensionless ratios stay float (EarningsEvent precedent)
    assert payload["overview"]["profit_margin"] == pytest.approx(0.152)


def test_statement_items_are_decimals() -> None:
    p = _period(date(2025, 9, 27), revenue="0.152")
    assert isinstance(p.items["revenue"], Decimal)
    assert str(p.items["revenue"]) == "0.152"
    assert p.model_dump(mode="json")["items"]["revenue"] == "0.152"


def test_insider_round_trip_signed_decimal() -> None:
    tx = InsiderTransaction(symbol="AAPL", name="Cook Timothy", title="CEO",
                            transaction_date=date(2026, 2, 26),
                            filing_date=date(2026, 3, 1), code="S",
                            shares_changed=Decimal("-3334"), price=Decimal("236.95"),
                            as_of=_AS_OF, source="finnhub")
    payload = tx.model_dump(mode="json")
    assert payload["shares_changed"] == "-3334"
    assert payload["price"] == "236.95"


# ----------------------------------------------------------- validators


def test_symbol_upper_validators() -> None:
    assert _report().symbol == "AAPL"
    assert FundamentalsReport(symbol=" aapl ", as_of=_AS_OF, source="s").symbol == "AAPL"
    filing = Filing(symbol="msft", form="10-K", filed=date(2025, 7, 30),
                    accession="0001564590-25-000001", as_of=_AS_OF, source="sec_edgar")
    assert filing.symbol == "MSFT"
    tx = InsiderTransaction(symbol="nvda", name="n", as_of=_AS_OF, source="s")
    assert tx.symbol == "NVDA"


def test_overview_fields_all_optional_default_none() -> None:
    ov = FundamentalsOverview()
    assert ov.name is None and ov.market_cap is None and ov.beta is None


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        FundamentalsOverview(bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        StatementPeriod(period="annual", fiscal_date_ending=date(2025, 9, 27),
                        surprise=True)  # type: ignore[call-arg]


def test_filing_defaults() -> None:
    filing = Filing(symbol="AAPL", form="8-K", filed=date(2026, 5, 1),
                    accession="0000320193-26-000042", as_of=_AS_OF, source="sec_edgar")
    assert filing.items == [] and filing.description is None
    assert filing.period_end is None and filing.url is None


# ----------------------------------------------------------- as_known_at (PIT)


def test_as_known_at_keeps_periods_filed_on_or_before_cutoff() -> None:
    report = _report(statements=[
        _period(date(2025, 9, 27), filed=date(2025, 11, 1), revenue="391035000000"),
        _period(date(2024, 9, 28), filed=date(2024, 11, 1), revenue="383285000000"),
    ])
    sliced = report.as_known_at(date(2025, 10, 31))
    assert [p.fiscal_date_ending for p in sliced.statements] == [date(2024, 9, 28)]
    # boundary: knowable exactly at the cutoff is KEPT (filed <= cutoff)
    assert len(report.as_known_at(date(2025, 11, 1)).statements) == 2


def test_as_known_at_unfiled_period_uses_90d_conservative_lag() -> None:
    # No filed date -> knowable at fiscal_date_ending + 90d, never earlier.
    report = _report(statements=[
        _period(date(2025, 6, 30), filed=None, period="quarterly", form=None,
                revenue="85000000000"),
    ])
    knowable = date(2025, 9, 28)  # 2025-06-30 + 90 days
    assert report.as_known_at(date(2025, 9, 27)).statements == []
    assert len(report.as_known_at(knowable).statements) == 1


def test_as_known_at_is_pure_and_preserves_other_fields() -> None:
    report = _report()
    sliced = report.as_known_at(date(2020, 1, 1))
    assert sliced.statements == [] and sliced is not report
    assert len(report.statements) == 1  # original untouched
    assert sliced.symbol == report.symbol and sliced.source == report.source
    assert sliced.overview == report.overview and sliced.as_of == report.as_of
