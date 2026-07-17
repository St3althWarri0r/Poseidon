# tests/unit/test_analysis_service.py
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.ai.analysis_service import AnalysisService
from poseidon.core.config import AnalysisConfig
from poseidon.core.models import Quote
from poseidon.storage.db import Database


class _Resp:
    def __init__(self, text):
        self.text = text
        self.model = "m"


class _Backend:
    model = "m"

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        if "facilitator" in system.lower():
            return _Resp('{"direction":"long","conviction":0.6,"synthesis":"s"}')
        return _Resp('{"stance":"bullish","confidence":0.6,"summary":"s",'
                     '"key_points":[],"data_gaps":[],"sources":[]}')


class _Router:
    async def quote(self, s, allow_delayed=True):
        return Quote(symbol="AAPL", last=Decimal("190.10"),
                     as_of=datetime.now(UTC), source="fake")

    async def bars(self, s, timeframe="1d", limit=30):
        return []


async def _svc(db, cfg):
    async def _audit(*a, **k):
        return None
    return AnalysisService(db=db, router=_Router(), config=cfg, model="m",
                           get_backend=lambda: _Backend(), watchlist=lambda: ["AAPL"],
                           audit_append=_audit, scan=None)


async def test_analyze_symbol_stores_one_packet(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    svc = await _svc(db, AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1))
    await svc.analyze_symbol("AAPL")
    got = await db.recent_packets(["AAPL"], refresh_hours=24, limit=3, now=datetime.now(UTC))
    assert len(got) == 1 and got[0].verdict.direction == "long"
    await db.close()


async def test_relevant_packets_gated_by_config(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    svc = await _svc(db, AnalysisConfig(enabled=True, inject=False))
    await (await _svc(db, AnalysisConfig(enabled=True, debate_rounds=1,
                                         risk_rounds=1))).analyze_symbol("AAPL")
    assert await svc.relevant_packets(["AAPL"]) == []   # inject=False -> nothing
    await db.close()


async def test_sweep_skips_fresh(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    svc = await _svc(db, AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1))
    await svc.run_sweep()
    await asyncio.gather(*svc._tasks)                    # drain the background task
    await svc.run_sweep()                                # second sweep: packet is fresh
    await asyncio.gather(*svc._tasks)
    # Assert on the raw table, not recent_packets() (which is LIMIT-1-per-symbol
    # and so returns one row whether the sweep recomputed or not) — this is the
    # only way to actually distinguish "skipped" from "recomputed".
    row = await db.fetch_one("SELECT COUNT(*) FROM analysis_packets")
    assert row is not None and row[0] == 1               # not recomputed
    await db.close()


async def test_sweep_recomputes_before_full_refresh_window_elapses(tmp_path) -> None:
    # A packet older than half the refresh window must be recomputed even
    # though it is still "fresh" under the full refresh_hours window (that
    # full window is the inject-staleness bound, not the recompute bound) —
    # otherwise a packet can go stale for injection before the sweep ever
    # refreshes it again.
    db = Database(tmp_path / "t.db")
    await db.open()
    cfg = AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1, refresh_hours=24)
    svc = await _svc(db, cfg)
    await svc.analyze_symbol("AAPL")
    stale = (datetime.now(UTC) - timedelta(hours=18)).isoformat()
    await db.execute("UPDATE analysis_packets SET as_of = ? WHERE symbol = ?", (stale, "AAPL"))
    await svc.run_sweep()
    await asyncio.gather(*svc._tasks)
    row = await db.fetch_one(
        "SELECT COUNT(*) FROM analysis_packets WHERE symbol = ?", ("AAPL",))
    assert row is not None and row[0] == 2               # recomputed: old row + new row
    await db.close()


async def test_sweep_skips_when_within_half_life(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    cfg = AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1, refresh_hours=24)
    svc = await _svc(db, cfg)
    await svc.analyze_symbol("AAPL")
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await db.execute("UPDATE analysis_packets SET as_of = ? WHERE symbol = ?", (recent, "AAPL"))
    await svc.run_sweep()
    await asyncio.gather(*svc._tasks)
    row = await db.fetch_one(
        "SELECT COUNT(*) FROM analysis_packets WHERE symbol = ?", ("AAPL",))
    assert row is not None and row[0] == 1               # still fresh -> skipped
    await db.close()
