# tests/unit/test_analysis_storage.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from poseidon.core.models import AnalysisPacket, AnalystReport, DebateVerdict, RiskLens
from poseidon.storage.db import Database


def _packet(symbol: str, as_of: datetime, pid: str) -> AnalysisPacket:
    return AnalysisPacket(
        id=pid, symbol=symbol, as_of=as_of, model="m",
        reports=[AnalystReport(role="news", summary="s", stance="neutral",
                               confidence=0.5, key_points=[], data_gaps=[], sources=[])],
        verdict=DebateVerdict(direction="avoid", conviction=0.4, bull_case="b",
                              bear_case="c", synthesis="s", rounds=1),
        risk_lens=RiskLens(aggressive="a", neutral="n", conservative="c", synthesis="s"),
        snapshot_digest="d")


async def test_store_and_fetch_fresh(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    now = datetime.now(UTC)
    await db.add_analysis_packet(_packet("AAPL", now, "p1"))
    assert await db.packet_fresh("AAPL", refresh_hours=24, now=now) is True
    got = await db.recent_packets(["AAPL"], refresh_hours=24, limit=3, now=now)
    assert len(got) == 1 and got[0].symbol == "AAPL"
    assert got[0].verdict.direction == "avoid"          # round-trips nested models
    await db.close()


async def test_stale_packet_excluded(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    now = datetime.now(UTC)
    await db.add_analysis_packet(_packet("MSFT", now - timedelta(hours=48), "p2"))
    assert await db.packet_fresh("MSFT", refresh_hours=24, now=now) is False
    assert await db.recent_packets(["MSFT"], refresh_hours=24, limit=3, now=now) == []
    await db.close()
