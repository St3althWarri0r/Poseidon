# tests/unit/test_analysis_analysts.py
from __future__ import annotations

from datetime import UTC, datetime

from poseidon.ai.analysis.analysts import _ROLES, run_analysts
from poseidon.ai.analysis.parse import first_json_obj
from poseidon.ai.analysis.snapshot import Snapshot


class _Resp:
    def __init__(self, text):
        self.text = text
        self.model = "m"


class _Backend:
    """Returns a valid analyst JSON for the first, junk for the rest — proves
    graceful degradation to neutral without crashing the fan-out."""
    def __init__(self): self.n = 0
    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            return _Resp('{"stance":"bullish","confidence":0.7,"summary":"ok",'
                         '"key_points":["p"],"data_gaps":[],"sources":["s"]}')
        return _Resp("not json at all")


def test_first_json_obj_extracts_from_prose() -> None:
    assert first_json_obj('prefix {"a": 1} suffix')["a"] == 1
    assert first_json_obj("no json here") == {}


def test_first_json_obj_ignores_braces_inside_strings() -> None:
    # A '}' inside a string value must not be counted by the balanced-brace
    # scan — it must not truncate extraction before the real closing brace.
    text = '{"a": "has } brace", "b": 2}'
    assert first_json_obj(text) == {"a": "has } brace", "b": 2}


def test_technical_role_carries_indicator_guidance() -> None:
    tech = _ROLES["technical"]
    assert "using ONLY the snapshot" in tech
    for phrase in (
        "SMA50/SMA200",
        "never time an entry off the cross alone",
        "EMA10",
        "MACD(12,26,9)",
        "RSI14",
        "walks the band",
        "ATR14",
        "NO directional content",
        "NEVER estimate a missing",
    ):
        assert phrase in tech


def test_guidance_absent_from_other_roles() -> None:
    for role in ("fundamentals", "news", "sentiment"):
        text = _ROLES[role]
        for phrase in ("walks the band", "NO directional content",
                       "SMA50/SMA200", "never time an entry off the cross alone"):
            assert phrase not in text


async def test_run_analysts_degrades_without_crashing() -> None:
    snap = Snapshot("AAPL", datetime.now(UTC), "fake", "AAPL last 190.10")
    reports = await run_analysts(_Backend(), snap, context="")
    assert len(reports) == 4                       # always four roles
    roles = {r.role for r in reports}
    assert roles == {"fundamentals", "technical", "news", "sentiment"}
    assert any(r.stance == "bullish" for r in reports)   # the valid one parsed
    assert all(r.stance in {"bullish", "bearish", "neutral"} for r in reports)  # junk -> neutral
