# tests/unit/test_analysis_service.py
from __future__ import annotations

from datetime import UTC, datetime

from poseidon.ai.analysis_service import AnalysisService
from poseidon.core.config import AnalysisConfig
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


class _Quote:
    price = 190.1
    as_of = datetime.now(UTC)
    source = "fake"


class _Router:
    async def quote(self, s, allow_delayed=True):
        return _Quote()

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
    await svc.run_sweep()                                # second sweep: packet is fresh
    got = await db.recent_packets(["AAPL"], refresh_hours=24, limit=9, now=datetime.now(UTC))
    assert len(got) == 1                                 # not recomputed
    await db.close()
