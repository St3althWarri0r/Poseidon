# tests/unit/test_analysis_models.py
from __future__ import annotations

from datetime import UTC, datetime

from poseidon.core.models import AnalysisPacket, AnalystReport, DebateVerdict, RiskLens


def _packet(**kw) -> AnalysisPacket:
    base = {
        "id": "p1", "symbol": "AAPL", "as_of": datetime.now(UTC), "model": "m",
        "reports": [AnalystReport(role="fundamentals", summary="ok", stance="bullish",
                               confidence=0.6, key_points=["a"], data_gaps=[], sources=["x"])],
        "verdict": DebateVerdict(direction="long", conviction=0.55, bull_case="b",
                              bear_case="c", synthesis="s", rounds=2),
        "risk_lens": RiskLens(aggressive="a", neutral="n", conservative="c", synthesis="s"),
        "snapshot_digest": "AAPL 190.10 ..."}
    base.update(kw)
    return AnalysisPacket(**base)


def test_render_is_hard_capped() -> None:
    p = _packet(verdict=DebateVerdict(direction="long", conviction=0.5,
                bull_case="B" * 5000, bear_case="C" * 5000, synthesis="S" * 5000, rounds=2))
    out = p.render(max_chars=1200)
    assert len(out) <= 1200
    assert "AAPL" in out            # symbol + direction survive the truncation
    assert "long" in out


def test_render_single_line_safe() -> None:
    # Plant the newline/control char in `conservative`, which render() DOES emit
    # (a field render() never reads would make this assertion vacuous).
    p = _packet(risk_lens=RiskLens(aggressive="a", neutral="n",
                                   conservative="danger\nline2\x07", synthesis="s"))
    out = p.render(max_chars=1200)
    assert "\x07" not in out        # control chars stripped (can't break framing)
    assert "danger" in out and "line2" in out   # content kept, collapsed to one line
