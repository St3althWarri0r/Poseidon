# tests/unit/test_fundamentals_wiring.py
"""AnalysisService fundamentals-context seam (r2-wave2 rank 4).

The callable seam is invoked once per analyze_symbol and its digest is
delivered to run_analysts as role_contexts={'fundamentals': ...}; an empty or
raising callable degrades to role_contexts=None (byte-identical prior
behavior) and NEVER prevents the packet write. Config-off equivalence: the
default AI stack keeps the identical module tool lists."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import poseidon.ai.analysis_service as analysis_service_module
from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.analysis_service import AnalysisService
from poseidon.ai.chat import ChatService
from poseidon.ai.schemas import ALL_TOOLS, DATA_TOOLS
from poseidon.core.config import AIConfig, AnalysisConfig
from poseidon.core.models import Quote
from poseidon.storage.db import Database


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "m"


class _Backend:
    model = "m"

    async def complete(self, messages, *, tools, system, force_tool=None,
                       max_tokens=None):
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


def _service(db: Database, fundamentals_context: Any) -> AnalysisService:
    async def _audit(*a: Any, **k: Any) -> None:
        return None

    return AnalysisService(
        db=db, router=_Router(), config=AnalysisConfig(enabled=True, debate_rounds=1,
                                                       risk_rounds=1),
        model="m", get_backend=lambda: _Backend(), watchlist=lambda: ["AAPL"],
        audit_append=_audit, scan=None, fundamentals_context=fundamentals_context)


def _capture_run_analysts(monkeypatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    original = analysis_service_module.run_analysts

    async def wrapper(backend, snapshot, *, context, scan=None, usage=None,
                      role_contexts=None):
        captured.append({"context": context, "role_contexts": role_contexts})
        return await original(backend, snapshot, context=context, scan=scan,
                              usage=usage, role_contexts=role_contexts)

    monkeypatch.setattr(analysis_service_module, "run_analysts", wrapper)
    return captured


async def test_seam_invoked_and_digest_delivered_as_role_context(
        tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fctx(symbol: str) -> str:
        calls.append(symbol)
        return "FUNDAMENTALS: revenue 391035000000"

    captured = _capture_run_analysts(monkeypatch)
    db = Database(tmp_path / "t.db")
    await db.open()
    await _service(db, fctx).analyze_symbol("AAPL")
    assert calls == ["AAPL"]
    assert captured[0]["role_contexts"] == {
        "fundamentals": "FUNDAMENTALS: revenue 391035000000"}
    got = await db.recent_packets(["AAPL"], refresh_hours=24, limit=3,
                                  now=datetime.now(UTC))
    assert len(got) == 1  # packet written as before
    await db.close()


async def test_raising_seam_degrades_to_none_and_packet_still_written(
        tmp_path, monkeypatch) -> None:
    async def fctx(symbol: str) -> str:
        raise RuntimeError("providers down")

    captured = _capture_run_analysts(monkeypatch)
    db = Database(tmp_path / "t.db")
    await db.open()
    await _service(db, fctx).analyze_symbol("AAPL")
    assert captured[0]["role_contexts"] is None  # best-effort degrade
    got = await db.recent_packets(["AAPL"], refresh_hours=24, limit=3,
                                  now=datetime.now(UTC))
    assert len(got) == 1  # a fundamentals outage never sinks the pipeline
    await db.close()


async def test_empty_digest_degrades_to_none(tmp_path, monkeypatch) -> None:
    async def fctx(symbol: str) -> str:
        return ""  # disabled surface returns '' — must not add an empty block

    captured = _capture_run_analysts(monkeypatch)
    db = Database(tmp_path / "t.db")
    await db.open()
    await _service(db, fctx).analyze_symbol("AAPL")
    assert captured[0]["role_contexts"] is None
    await db.close()


async def test_no_seam_behaves_exactly_as_today(tmp_path, monkeypatch) -> None:
    captured = _capture_run_analysts(monkeypatch)
    db = Database(tmp_path / "t.db")
    await db.open()
    await _service(db, None).analyze_symbol("AAPL")
    assert captured[0] == {"context": "", "role_contexts": None}
    await db.close()


def test_config_off_equivalence_module_tool_objects() -> None:
    cfg = AIConfig()
    assert ClaudeAgent(cfg, None, None)._tools is ALL_TOOLS  # type: ignore[arg-type]
    assert ChatService(cfg, None, None, None)._tools is DATA_TOOLS  # type: ignore[arg-type]
