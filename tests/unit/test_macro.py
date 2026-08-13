"""Macro regime context: VIX + Treasury curve (r3 rank 3).

The properties that matter, in order: macro is CONTEXT and never price truth;
CBOE's feed is delayed and must say so; and the two legs degrade
INDEPENDENTLY, because losing optional context must never cost the PM a cycle.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from poseidon.core.errors import DataError
from poseidon.data.macro import (
    MacroSnapshot,
    fetch_macro_snapshot,
    fetch_vix,
    parse_vix,
    vix_regime,
)

_VIX_DOC: dict[str, Any] = {
    "timestamp": "2026-08-13 05:09:05",
    "data": {
        "symbol": "^VIX", "security_type": "index", "current_price": 14.55,
        "price_change": -0.73, "price_change_percent": -5.0172,
        "open": 14.92, "high": 14.96, "low": 14.39, "close": 14.55,
        "last_trade_time": "2026-08-12T16:15:01",
    },
    "symbol": "_VIX",
}

_NS = ('xmlns="http://www.w3.org/2005/Atom" '
       'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" '
       'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"')
_CURVE = (f'<?xml version="1.0" encoding="utf-8"?><feed {_NS}>'
          '<entry><content><m:properties>'
          '<d:NEW_DATE>2026-08-12T00:00:00</d:NEW_DATE>'
          '<d:BC_3MONTH>3.87</d:BC_3MONTH><d:BC_10YEAR>4.68</d:BC_10YEAR>'
          '</m:properties></content></entry></feed>')


def _transport(*, vix: httpx.Response | None = None,
               curve: httpx.Response | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "cboe" in request.url.host:
            if vix is None:
                raise httpx.ConnectError("cboe down")
            return vix
        if curve is None:
            raise httpx.ConnectError("treasury down")
        return curve

    return httpx.MockTransport(handler)


def _ok_vix() -> httpx.Response:
    return httpx.Response(200, content=json.dumps(_VIX_DOC).encode(),
                          headers={"content-type": "application/json"})


def _ok_curve() -> httpx.Response:
    return httpx.Response(200, text=_CURVE)


# ------------------------------------------------------------------- parsing


def test_parses_the_index_level_and_change() -> None:
    quote = parse_vix(_VIX_DOC)
    assert quote.level == pytest.approx(14.55)
    assert quote.change_percent == pytest.approx(-5.0172)
    assert quote.delayed is True  # CBOE's public endpoint is delayed, always


def test_falls_back_to_close_when_current_price_is_absent() -> None:
    doc = {"data": {"close": 21.4}}
    assert parse_vix(doc).level == pytest.approx(21.4)


@pytest.mark.parametrize("doc", [
    {},                                  # no data object
    {"data": {}},                        # no level at all
    {"data": {"current_price": "n/a"}},  # unparseable
    {"data": {"current_price": 0}},      # a zero VIX would read as "no volatility"
    {"data": {"current_price": -3}},
    [],                                  # not an object
])
def test_unusable_payloads_raise_a_data_error(doc: Any) -> None:
    with pytest.raises(DataError):
        parse_vix(doc)


@pytest.mark.parametrize(("level", "label"), [
    (9.5, "very_low"), (14.55, "low"), (25.0, "elevated"),
    (33.0, "high"), (80.0, "extreme"),
])
def test_regime_bands(level: float, label: str) -> None:
    assert vix_regime(level) == label


# ------------------------------------------------------------------ fetching


async def test_fetch_vix_reads_the_live_shape() -> None:
    quote = await fetch_vix(transport=_transport(vix=_ok_vix()), cache={})
    assert quote.level == pytest.approx(14.55)


async def test_fetch_vix_is_cached() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_vix()

    cache: dict[str, Any] = {}
    transport = httpx.MockTransport(handler)
    await fetch_vix(transport=transport, cache=cache)
    await fetch_vix(transport=transport, cache=cache)
    assert len(calls) == 1


async def test_http_error_raises_a_data_error() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(503, text="down"))
    with pytest.raises(DataError):
        await fetch_vix(transport=transport, cache={})


# ------------------------------------------------------------------ snapshot


async def test_snapshot_composes_both_legs() -> None:
    snap = await fetch_macro_snapshot(
        on=date(2026, 8, 12), cache={},
        transport=_transport(vix=_ok_vix(), curve=_ok_curve()))
    assert snap.vix is not None and snap.vix.level == pytest.approx(14.55)
    assert snap.vix_regime == "low"
    assert snap.curve_as_of == date(2026, 8, 12)
    assert snap.term_spread == pytest.approx(0.0468 - 0.0387)
    assert snap.curve_inverted is False
    assert snap.gaps == []


async def test_an_inverted_curve_is_flagged() -> None:
    inverted = _CURVE.replace("<d:BC_10YEAR>4.68</d:BC_10YEAR>",
                              "<d:BC_10YEAR>3.40</d:BC_10YEAR>")
    snap = await fetch_macro_snapshot(
        on=date(2026, 8, 12), cache={},
        transport=_transport(vix=_ok_vix(), curve=httpx.Response(200, text=inverted)))
    assert snap.term_spread is not None and snap.term_spread < 0
    assert snap.curve_inverted is True


async def test_a_dead_vix_leg_does_not_cost_the_yield_curve() -> None:
    snap = await fetch_macro_snapshot(
        on=date(2026, 8, 12), cache={}, transport=_transport(curve=_ok_curve()))
    assert snap.vix is None
    assert snap.term_spread is not None  # the surviving leg still reports
    assert any("vix_unavailable" in g for g in snap.gaps)


async def test_a_dead_treasury_leg_does_not_cost_vix() -> None:
    snap = await fetch_macro_snapshot(
        on=date(2026, 8, 12), cache={}, transport=_transport(vix=_ok_vix()))
    assert snap.vix is not None
    assert snap.yield_curve == {}
    assert snap.term_spread is None  # a gap, NOT a flat curve
    assert any("yield_curve_unavailable" in g for g in snap.gaps)


async def test_both_legs_down_still_returns_a_snapshot_not_an_exception() -> None:
    # Optional context must never take down a cycle.
    snap = await fetch_macro_snapshot(on=date(2026, 8, 12), cache={},
                                      transport=_transport())
    assert isinstance(snap, MacroSnapshot)
    assert snap.vix is None and snap.yield_curve == {}
    assert len(snap.gaps) == 2


# -------------------------------------------------------------- payload shape


async def test_payload_labels_vix_delayed_and_disclaims_price_truth() -> None:
    snap = await fetch_macro_snapshot(
        on=date(2026, 8, 12), cache={},
        transport=_transport(vix=_ok_vix(), curve=_ok_curve()))
    payload = snap.as_dict()
    assert payload["vix"]["freshness"] == "delayed"
    note = payload["note"].lower()
    assert "not price truth" in note or "context" in note
    assert "get_quote" in note
    assert "gaps" not in payload  # only present when something is actually missing


async def test_payload_reports_gaps_explicitly() -> None:
    snap = await fetch_macro_snapshot(on=date(2026, 8, 12), cache={},
                                      transport=_transport(curve=_ok_curve()))
    payload = snap.as_dict()
    assert payload["vix"] is None
    assert payload["vix_regime"] is None
    assert any("vix_unavailable" in g for g in payload["gaps"])
