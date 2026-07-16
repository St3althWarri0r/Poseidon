# tests/unit/test_audit_analysis.py — audit findings 9 & 23 (analysis service).
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from poseidon.ai.analysis_service import AnalysisService
from poseidon.core.config import AnalysisConfig
from poseidon.storage.db import Database


class _Resp:
    def __init__(self, text):
        self.text = text
        self.model = "m"


class _Quote:
    price = 190.1
    as_of = datetime.now(UTC)
    source = "fake"


class _Router:
    async def quote(self, s, allow_delayed=True):
        return _Quote()

    async def bars(self, s, timeframe="1d", limit=30):
        return []


class _BlockingBackend:
    """Holds every completion until released, keeping analyze_symbol in flight."""

    model = "m"

    def __init__(self):
        self.release = asyncio.Event()

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        await self.release.wait()
        if "facilitator" in system.lower():
            return _Resp('{"direction":"long","conviction":0.6,"synthesis":"s"}')
        return _Resp('{"stance":"bullish","confidence":0.6,"summary":"s",'
                     '"key_points":[],"data_gaps":[],"sources":[]}')


class _DownBackend:
    """Utility backend outage: every completion fails, every stage degrades."""

    model = "m"

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        raise RuntimeError("utility backend unreachable")


class _PartialBackend:
    """Analysts fail but the debate stage still works: a partially degraded run."""

    model = "m"

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        low = system.lower()
        if "facilitator" in low:
            return _Resp('{"direction":"avoid","conviction":0.2,"synthesis":"mixed picture"}')
        if "analyst" in low:
            raise RuntimeError("analyst tier unreachable")
        return _Resp("case text")                # debate turns / risk voices succeed


def _svc(db, cfg, backend):
    async def _audit(*a, **k):
        return None
    return AnalysisService(db=db, router=_Router(), config=cfg, model="m",
                           get_backend=lambda: backend, watchlist=lambda: ["AAPL"],
                           audit_append=_audit, scan=None)


async def _packet_count(db):
    row = await db.fetch_one("SELECT COUNT(*) FROM analysis_packets")
    assert row is not None
    return row[0]


async def test_overlapping_sweeps_compute_each_symbol_once(tmp_path) -> None:
    # Finding 9: packet_fresh only sees *written* packets, so a second sweep
    # tick that fires while the first tick's pipeline is still running must be
    # gated by in-flight tracking, not spawn a duplicate pipeline.
    db = Database(tmp_path / "t.db")
    await db.open()
    backend = _BlockingBackend()
    svc = _svc(db, AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1), backend)
    await svc.run_sweep()
    await asyncio.sleep(0)                    # let tick 1's task start and block
    await svc.run_sweep()                     # tick 2 fires while work is in flight
    backend.release.set()
    await asyncio.gather(*svc._tasks)
    assert await _packet_count(db) == 1       # one computation, not one per tick
    assert svc._inflight == set()             # gate released once the work is done
    await db.close()


async def test_full_outage_persists_nothing(tmp_path) -> None:
    # Finding 23: a fully degraded run (every stage failed) must not mint an
    # empty 'avoid' packet — persisting one would inject noise as the firm view
    # and block recomputation for the refresh half-window after recovery.
    db = Database(tmp_path / "t.db")
    await db.open()
    svc = _svc(db, AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1),
               _DownBackend())
    await svc.analyze_symbol("AAPL")
    assert await _packet_count(db) == 0
    # Nothing persisted -> the recompute window is not consumed.
    assert not await db.packet_fresh("AAPL", refresh_hours=12, now=datetime.now(UTC))
    await db.close()


async def test_partial_degradation_is_kept_and_flagged(tmp_path) -> None:
    # Partial degradation (analysts down, debate up) still carries signal: the
    # packet is persisted with the degraded reports flagged via data_gaps.
    db = Database(tmp_path / "t.db")
    await db.open()
    svc = _svc(db, AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1),
               _PartialBackend())
    await svc.analyze_symbol("AAPL")
    got = await db.recent_packets(["AAPL"], refresh_hours=24, limit=3, now=datetime.now(UTC))
    assert len(got) == 1
    assert got[0].verdict.synthesis == "mixed picture"
    assert all("analyst unavailable" in " ".join(r.data_gaps) for r in got[0].reports)
    await db.close()
