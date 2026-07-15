# tests/unit/test_analysis_packet_assemble.py
from __future__ import annotations

from datetime import UTC, datetime

from poseidon.ai.analysis.packet import assemble
from poseidon.ai.analysis.snapshot import Snapshot
from poseidon.core.models import AnalystReport, DebateVerdict, RiskLens


async def test_assemble_builds_packet() -> None:
    snap = Snapshot("AAPL", datetime.now(UTC), "fake", "AAPL last 190.10")
    reports = [AnalystReport(role="news", summary="s", stance="neutral", confidence=0.5,
                             key_points=[], data_gaps=[], sources=[])]
    verdict = DebateVerdict(direction="long", conviction=0.6, bull_case="b", bear_case="c",
                            synthesis="s", rounds=2)
    lens = RiskLens(aggressive="a", neutral="n", conservative="c", synthesis="s")
    p = assemble(packet_id="p1", symbol="AAPL", snapshot=snap, reports=reports,
                 verdict=verdict, risk_lens=lens, model="m")
    assert p.symbol == "AAPL" and p.model == "m"
    assert p.snapshot_digest == snap.text and p.as_of == snap.as_of
    assert p.render(1200)                          # renders without error
