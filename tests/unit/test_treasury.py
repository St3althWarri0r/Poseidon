"""US Treasury par yield curve loader (r3 ranks 1 and 3).

This is Poseidon's risk-free rate source and its first macro state series.
The Ken French RF column was considered and rejected for the rate: it carries
two decimals of DAILY percent, so it quantizes to 2.52%/yr steps (2023, 2024
and 2025 all report exactly 5.04%). Treasury publishes the ANNUAL percent to
two decimals with a one-day lag, which is the precision the number needs.

No network here — the parser is pure and the fetch runs over an
``httpx.MockTransport`` serving a synthetic Atom/OData feed shaped like the
real one, namespaces and all.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from poseidon.data.treasury import (
    TENOR_3M,
    YieldCurveRow,
    fetch_yield_curve,
    parse_yield_curve_xml,
    risk_free_annual_on,
    term_spread,
)

_NS = ('xmlns="http://www.w3.org/2005/Atom" '
       'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" '
       'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"')


def _entry(day: str, **tenors: str) -> str:
    body = "".join(f"<d:{k}>{v}</d:{k}>" for k, v in tenors.items())
    return (f"<entry><content><m:properties>"
            f"<d:NEW_DATE>{day}T00:00:00</d:NEW_DATE>{body}"
            f"</m:properties></content></entry>")


_FEED = (f'<?xml version="1.0" encoding="utf-8"?><feed {_NS}>'
         + _entry("2026-08-11", BC_1MONTH="3.75", BC_3MONTH="3.85",
                  BC_2YEAR="4.18", BC_10YEAR="4.65", BC_30YEAR="5.20")
         + _entry("2026-08-12", BC_1MONTH="3.78", BC_3MONTH="3.87",
                  BC_2YEAR="4.20", BC_10YEAR="4.68", BC_30YEAR="5.24")
         + "</feed>")


# ------------------------------------------------------------------- parsing


def test_parses_entries_into_decimal_yields() -> None:
    rows = parse_yield_curve_xml(_FEED)
    assert len(rows) == 2
    last = rows[-1]
    assert last.day == date(2026, 8, 12)
    # Treasury publishes ANNUAL percent: 3.87 means 3.87%/yr, i.e. 0.0387.
    assert last.yields[TENOR_3M] == pytest.approx(0.0387)
    assert last.yields["10Y"] == pytest.approx(0.0468)


def test_rows_are_sorted_by_date() -> None:
    reversed_feed = (f'<?xml version="1.0" encoding="utf-8"?><feed {_NS}>'
                     + _entry("2026-08-12", BC_3MONTH="3.87")
                     + _entry("2026-08-11", BC_3MONTH="3.85")
                     + "</feed>")
    rows = parse_yield_curve_xml(reversed_feed)
    assert [r.day for r in rows] == [date(2026, 8, 11), date(2026, 8, 12)]


def test_missing_and_unparseable_tenors_are_skipped_not_fatal() -> None:
    # Treasury omits tenors that did not trade (the 30Y was absent for years),
    # and publishes empty elements rather than zeros. A gap is not a 0% yield.
    feed = (f'<?xml version="1.0" encoding="utf-8"?><feed {_NS}>'
            + _entry("2026-08-12", BC_3MONTH="3.87", BC_30YEAR="", BC_10YEAR="n/a")
            + "</feed>")
    rows = parse_yield_curve_xml(feed)
    assert rows[0].yields[TENOR_3M] == pytest.approx(0.0387)
    assert "30Y" not in rows[0].yields
    assert "10Y" not in rows[0].yields


def test_entry_without_a_date_is_dropped() -> None:
    feed = (f'<?xml version="1.0" encoding="utf-8"?><feed {_NS}>'
            '<entry><content><m:properties><d:BC_3MONTH>3.87</d:BC_3MONTH>'
            '</m:properties></content></entry>' + "</feed>")
    assert parse_yield_curve_xml(feed) == []


def test_malformed_xml_raises_a_data_error() -> None:
    from poseidon.core.errors import DataError

    with pytest.raises(DataError):
        parse_yield_curve_xml("<feed><unclosed>")


# --------------------------------------------------------- risk-free lookup


def _rows() -> list[YieldCurveRow]:
    return parse_yield_curve_xml(_FEED)


def test_risk_free_uses_the_three_month_yield() -> None:
    assert risk_free_annual_on(_rows(), date(2026, 8, 12)) == pytest.approx(0.0387)


def test_risk_free_carries_the_last_curve_forward() -> None:
    # Weekends, holidays and the one-day publication lag all land here.
    assert risk_free_annual_on(_rows(), date(2026, 8, 20)) == pytest.approx(0.0387)


def test_risk_free_uses_the_curve_in_effect_not_a_later_one() -> None:
    # Look-ahead guard: a backtest bar on the 11th must not see the 12th's rate.
    assert risk_free_annual_on(_rows(), date(2026, 8, 11)) == pytest.approx(0.0385)


def test_risk_free_before_the_series_is_zero() -> None:
    assert risk_free_annual_on(_rows(), date(2020, 1, 1)) == 0.0


def test_risk_free_on_empty_series_is_zero() -> None:
    assert risk_free_annual_on([], date(2026, 8, 12)) == 0.0


# ------------------------------------------------------------- term spread


def test_term_spread_is_ten_year_minus_three_month() -> None:
    assert term_spread(_rows()[-1]) == pytest.approx(0.0468 - 0.0387)


def test_term_spread_is_none_when_a_leg_is_missing() -> None:
    row = parse_yield_curve_xml(
        f'<?xml version="1.0" encoding="utf-8"?><feed {_NS}>'
        + _entry("2026-08-12", BC_3MONTH="3.87") + "</feed>")[0]
    assert term_spread(row) is None


# ------------------------------------------------------------------ fetching


async def test_fetch_parses_the_requested_year() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=_FEED)

    rows = await fetch_yield_curve(year=2026, transport=httpx.MockTransport(handler),
                                   cache={})
    assert len(rows) == 2
    assert "field_tdr_date_value=2026" in str(seen[0].url)


async def test_fetch_is_cached_per_year_within_the_ttl() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=_FEED)

    transport = httpx.MockTransport(handler)
    cache: dict[str, object] = {}
    await fetch_yield_curve(year=2026, transport=transport, cache=cache)
    await fetch_yield_curve(year=2026, transport=transport, cache=cache)
    assert len(calls) == 1
    # A different year is a different key, not a cache hit.
    await fetch_yield_curve(year=2025, transport=transport, cache=cache)
    assert len(calls) == 2


async def test_fetch_failure_raises_a_data_error() -> None:
    from poseidon.core.errors import DataError

    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="boom"))
    with pytest.raises(DataError):
        await fetch_yield_curve(year=2026, transport=transport, cache={})
