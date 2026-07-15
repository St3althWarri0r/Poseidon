# tests/unit/test_analysis_debate.py
from __future__ import annotations

from poseidon.ai.analysis.debate import run_debate
from poseidon.core.models import AnalystReport


class _Resp:
    def __init__(self, text):
        self.text = text
        self.model = "m"


class _Backend:
    def __init__(self, facilitator_json):
        self._fac = facilitator_json
        self.calls = 0

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        self.calls += 1
        if "facilitator" in system.lower():
            return _Resp(self._fac)
        return _Resp("some argument")


def _reports():
    return [AnalystReport(role="technical", summary="up", stance="bullish",
                          confidence=0.7, key_points=[], data_gaps=[], sources=[])]


async def test_debate_returns_structured_verdict() -> None:
    b = _Backend('{"direction":"long","conviction":0.6,"synthesis":"bull wins"}')
    v = await run_debate(b, _reports(), rounds=2)
    assert v.direction == "long" and 0.0 <= v.conviction <= 1.0
    assert v.rounds == 2 and b.calls >= 3          # 2 rounds x (bull+bear) + facilitator


async def test_debate_degrades_on_bad_facilitator() -> None:
    v = await run_debate(_Backend("not json"), _reports(), rounds=1)
    assert v.direction == "avoid" and v.conviction == 0.0
