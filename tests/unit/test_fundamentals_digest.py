# tests/unit/test_fundamentals_digest.py
"""Fundamentals analyst digest (r2-wave2 rank 4).

render_fundamentals_digest is pure and deterministic: Decimal strings
verbatim, absent fields omitted (never estimated), the free-prose description
EXCLUDED (numbers and taxonomy only — injection-surface minimization), capped
to max_chars on a whole-line boundary. fundamentals_context is best-effort:
'' when the gate is off (zero router calls) and '' on any failure."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from poseidon.ai.analysis.fundamentals import fundamentals_context, render_fundamentals_digest
from poseidon.core.config import FundamentalsConfig
from poseidon.core.errors import DataUnavailableError
from poseidon.core.models import FundamentalsOverview, FundamentalsReport, StatementPeriod

_AS_OF = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _report(*, periods: int = 2, description: str | None = "SECRET PROSE",
            overview: bool = True) -> FundamentalsReport:
    statements = [
        StatementPeriod(period="annual", fiscal_date_ending=date(2025 - i, 9, 27),
                        filed=date(2025 - i, 11, 1), form="10-K", currency="USD",
                        items={"revenue": Decimal("391035000000"),
                               "net_income": Decimal("93736000000")})
        for i in range(periods)
    ]
    return FundamentalsReport(
        symbol="AAPL",
        overview=FundamentalsOverview(
            name="Apple Inc.", sector="Technology", description=description,
            market_cap=Decimal("3400120000000"), eps_ttm=Decimal("6.42"),
            pe_ratio=34.2) if overview else None,
        statements=statements, as_of=_AS_OF, source="sec_edgar")


# ----------------------------------------------------------- digest


def test_digest_pins_exact_numbers_and_provenance() -> None:
    digest = render_fundamentals_digest(_report(), max_chars=900)
    assert digest.startswith(
        f"FUNDAMENTALS (filed/reported data; source sec_edgar, as_of {_AS_OF.isoformat()}):")
    assert "Apple Inc." in digest and "Technology" in digest
    assert "market_cap 3400120000000" in digest  # str(Decimal) verbatim
    assert "eps_ttm 6.42" in digest
    assert "pe 34.2000" in digest  # float ratios to 4dp
    assert "10-K FY end 2025-09-27 filed 2025-11-01: revenue 391035000000" in digest
    assert "net_income 93736000000" in digest


def test_digest_excludes_description_prose() -> None:
    # The free-prose field is the injection surface — never in the digest.
    assert "SECRET PROSE" not in render_fundamentals_digest(_report(), max_chars=2000)


def test_digest_omits_absent_fields_never_estimates() -> None:
    digest = render_fundamentals_digest(_report(overview=False), max_chars=900)
    assert "market_cap" not in digest and "pe " not in digest
    assert "None" not in digest and "estimate" not in digest
    # statements still render without an overview
    assert "10-K FY end 2025-09-27" in digest


def test_digest_caps_at_three_newest_periods() -> None:
    digest = render_fundamentals_digest(_report(periods=6), max_chars=5000)
    assert digest.count("10-K FY end") == 3
    assert "2025-09-27" in digest and "2023-09-27" in digest
    assert "2022-09-27" not in digest  # older periods dropped


def test_digest_max_chars_respected_on_whole_line_boundary() -> None:
    full = render_fundamentals_digest(_report(periods=3), max_chars=5000)
    capped = render_fundamentals_digest(_report(periods=3), max_chars=250)
    assert len(capped) <= 250
    # ends on a whole line: every emitted line is a complete line of the full render
    assert all(line in full.split("\n") for line in capped.split("\n"))


def test_digest_deterministic() -> None:
    a = render_fundamentals_digest(_report(), max_chars=900)
    b = render_fundamentals_digest(_report(), max_chars=900)
    assert a == b  # byte-equal across calls


def test_unfiled_quarterly_line_renders_without_filed() -> None:
    report = FundamentalsReport(
        symbol="AAPL",
        statements=[StatementPeriod(period="quarterly",
                                    fiscal_date_ending=date(2025, 6, 28),
                                    items={"revenue": Decimal("85777000000")})],
        as_of=_AS_OF, source="alphavantage")
    digest = render_fundamentals_digest(report, max_chars=900)
    # AV publishes no filed date — the period line omits it, never invents one.
    assert digest.split("\n")[-1] == "quarterly Q end 2025-06-28: revenue 85777000000"


# ----------------------------------------------------------- fundamentals_context


class _Router:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self._raises = raises

    async def fundamentals(self, symbol: str) -> FundamentalsReport:
        self.calls += 1
        if self._raises:
            raise DataUnavailableError("all providers down")
        return _report()


async def test_context_disabled_returns_empty_with_zero_router_calls() -> None:
    router = _Router()
    for cfg in (FundamentalsConfig(),  # enabled=False (ships)
                FundamentalsConfig(enabled=True, analyst_context=False),
                FundamentalsConfig(enabled=False, analyst_context=True)):
        assert await fundamentals_context(router, "AAPL", cfg) == ""  # type: ignore[arg-type]
    assert router.calls == 0


async def test_context_failure_returns_empty_never_raises() -> None:
    router = _Router(raises=True)
    cfg = FundamentalsConfig(enabled=True)
    assert await fundamentals_context(router, "AAPL", cfg) == ""  # type: ignore[arg-type]
    assert router.calls == 1


async def test_context_enabled_returns_bounded_digest() -> None:
    cfg = FundamentalsConfig(enabled=True, digest_max_chars=300)
    digest = await fundamentals_context(_Router(), "AAPL", cfg)  # type: ignore[arg-type]
    assert digest.startswith("FUNDAMENTALS (filed/reported data;")
    assert len(digest) <= 300
