"""Rendering contract for the decision report's risk case."""

from __future__ import annotations

from typing import Any

from poseidon.ai.reports import render_decision_report
from poseidon.core.models import Decision

from .test_agent_parsing import RATIONALE, make_agent


def _decision(rationale: dict[str, Any]) -> Decision:
    return make_agent()._parse_decision(
        {"action": "hold", "trades": [], "rationale": rationale,
         "data_gaps": [], "summary": "s"},
        "c1", "m",
    )


def test_report_renders_invalidation_when_present() -> None:
    out = render_decision_report(_decision({**RATIONALE, "invalidation": "breaks 95 support"}))
    assert "Invalidates" in out
    assert "breaks 95 support" in out


def test_report_omits_invalidation_when_absent() -> None:
    # Old stored decisions render without a dangling empty field.
    assert "Invalidates" not in render_decision_report(_decision(dict(RATIONALE)))
