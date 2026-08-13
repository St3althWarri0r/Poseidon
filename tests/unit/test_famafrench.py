"""Ken French factor-library loader (r3 rank 1/2).

The library is the keyless source for two things Poseidon lacked: a REAL
risk-free rate (killing the rf=0 Sharpe overstatement) and the canonical
Mkt-RF/SMB/HML factor returns to regress candidate factors against.

No network here — the parser is pure and the fetch runs over an
``httpx.MockTransport`` serving a synthetic zip built to match the real
file's shape exactly: latin-1, several preamble lines, a bare
``,Mkt-RF,SMB,HML,RF`` header, ``YYYYMMDD`` keys, values in PERCENT, then a
blank line and a copyright footer.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import httpx
import pytest

from poseidon.data.famafrench import (
    FactorRow,
    fetch_factor_rows,
    parse_factor_csv,
    risk_free_annual_on,
)

_PREAMBLE = (
    "This file was created by using the 202606 CRSP database.\n"
    "The Tbill return is the simple daily rate that, over the number of trading days\n"
    "compounds to 1-month TBill rate.\n"
    "\n"
)
_ROWS = (
    "19260701,    0.09,   -0.25,   -0.27,    0.01\n"
    "19260702,    0.44,   -0.33,   -0.06,    0.01\n"
    "20260629,    0.51,    0.22,   -0.40,    0.02\n"
    "20260630,    0.73,    0.10,   -0.62,    0.01\n"
)
_FOOTER = "\nCopyright 2026 Eugene F. Fama and Kenneth R. French\n"
_CSV = _PREAMBLE + ",Mkt-RF,SMB,HML,RF\n" + _ROWS + _FOOTER


def _zip_bytes(csv_text: str = _CSV) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("F-F_Research_Data_Factors_daily.csv", csv_text.encode("latin-1"))
    return buf.getvalue()


# ------------------------------------------------------------------- parsing


def test_parses_rows_and_converts_percent_to_decimal() -> None:
    rows = parse_factor_csv(_CSV)
    assert len(rows) == 4
    first = rows[0]
    assert first.day == date(1926, 7, 1)
    # The file publishes PERCENT: 0.09 means 0.09%, i.e. 0.0009. Skipping the
    # /100 would inflate every factor and rate by two orders of magnitude.
    assert first.mkt_rf == pytest.approx(0.0009)
    assert first.smb == pytest.approx(-0.0025)
    assert first.hml == pytest.approx(-0.0027)
    assert first.rf == pytest.approx(0.0001)


def test_preamble_footer_and_blank_lines_are_skipped() -> None:
    rows = parse_factor_csv(_CSV)
    assert [r.day for r in rows] == [
        date(1926, 7, 1), date(1926, 7, 2), date(2026, 6, 29), date(2026, 6, 30)]


def test_rows_are_sorted_and_malformed_lines_are_dropped() -> None:
    messy = (",Mkt-RF,SMB,HML,RF\n"
             "20260630,    0.73,    0.10,   -0.62,    0.01\n"
             "notadate,  0.1, 0.1, 0.1, 0.1\n"
             "20260101,   -0.20,    0.05,    0.11,    0.02\n"
             "20260102,   x,  0.1, 0.1, 0.1\n"        # unparseable value
             "20260103,   0.1, 0.1\n")                # too few columns
    rows = parse_factor_csv(messy)
    assert [r.day for r in rows] == [date(2026, 1, 1), date(2026, 6, 30)]


def test_annual_monthly_section_is_not_mixed_into_the_daily_series() -> None:
    # The real monthly/annual files append a second table under a repeated
    # header. A 4-digit "date" is a YEAR, never a day, and must be dropped.
    text = (",Mkt-RF,SMB,HML,RF\n"
            "20260630,    0.73,    0.10,   -0.62,    0.01\n"
            "\n Annual Factors: January-December \n"
            ",Mkt-RF,SMB,HML,RF\n"
            "2025,   12.10,   -1.20,    3.40,    4.90\n")
    rows = parse_factor_csv(text)
    assert [r.day for r in rows] == [date(2026, 6, 30)]


# --------------------------------------------------------- risk-free lookup


def _rows() -> list[FactorRow]:
    return parse_factor_csv(_CSV)


def test_risk_free_annualizes_the_daily_rate() -> None:
    # The RF column is a DAILY simple rate; stats.sharpe_ratio divides its
    # argument by 252, so the loader must multiply by the same constant.
    got = risk_free_annual_on(_rows(), date(2026, 6, 30))
    assert got == pytest.approx(0.0001 * 252.0)


def test_risk_free_carries_the_last_known_rate_forward() -> None:
    # Ken French publishes ~6 weeks in arrears, so live-adjacent dates are
    # ALWAYS past the end of the file. Carrying forward is the honest answer;
    # silently returning 0.0 would resurrect the very bug this fixes.
    assert risk_free_annual_on(_rows(), date(2026, 8, 13)) == pytest.approx(0.0001 * 252.0)


def test_risk_free_uses_the_most_recent_row_at_or_before_the_date() -> None:
    assert risk_free_annual_on(_rows(), date(2026, 6, 29)) == pytest.approx(0.0002 * 252.0)


def test_risk_free_before_the_series_starts_is_zero() -> None:
    # No data is no data: 0.0 is the only honest answer before 1926.
    assert risk_free_annual_on(_rows(), date(1900, 1, 1)) == 0.0


def test_risk_free_on_empty_series_is_zero() -> None:
    assert risk_free_annual_on([], date(2026, 6, 30)) == 0.0


# ------------------------------------------------------------------ fetching


async def test_fetch_unzips_and_parses() -> None:
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, content=_zip_bytes()))
    rows = await fetch_factor_rows(transport=transport, cache={})
    assert len(rows) == 4
    assert rows[-1].day == date(2026, 6, 30)


async def test_fetch_result_is_cached_within_the_ttl() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=_zip_bytes())

    transport = httpx.MockTransport(handler)
    cache: dict[str, object] = {}
    await fetch_factor_rows(transport=transport, cache=cache)
    await fetch_factor_rows(transport=transport, cache=cache)
    assert len(calls) == 1  # the 178 KB download happens once per TTL


async def test_fetch_failure_raises_a_data_error() -> None:
    # Each of these passes its OWN cache: the module-level one is a real
    # process-wide cache, so sharing it here would let an earlier test's
    # success satisfy a later test's error path.
    from poseidon.core.errors import DataError

    transport = httpx.MockTransport(lambda _req: httpx.Response(503, content=b"down"))
    with pytest.raises(DataError):
        await fetch_factor_rows(transport=transport, cache={})


async def test_non_zip_payload_raises_a_data_error() -> None:
    from poseidon.core.errors import DataError

    transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(DataError):
        await fetch_factor_rows(transport=transport, cache={})
