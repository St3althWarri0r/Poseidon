# tests/unit/test_analysis_risk_lens.py
from __future__ import annotations

from poseidon.ai.analysis.risk_lens import run_risk_lens
from poseidon.core.models import DebateVerdict


class _Resp:
    def __init__(self, text):
        self.text = text
        self.model = "m"


class _Backend:
    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        return _Resp("advisory commentary")


async def test_risk_lens_has_three_voices() -> None:
    v = DebateVerdict(direction="long", conviction=0.6, bull_case="b", bear_case="c",
                      synthesis="s", rounds=1)
    lens = await run_risk_lens(_Backend(), v, [], rounds=1)
    assert lens.aggressive and lens.neutral and lens.conservative


async def test_risk_lens_degrades() -> None:
    class _Dead:
        async def complete(self, *a, **k):
            raise RuntimeError("x")
    v = DebateVerdict(direction="avoid", conviction=0.0, bull_case="", bear_case="",
                      synthesis="", rounds=1)
    lens = await run_risk_lens(_Dead(), v, [], rounds=1)
    assert lens.aggressive == "" and lens.synthesis == ""   # empty, no crash
