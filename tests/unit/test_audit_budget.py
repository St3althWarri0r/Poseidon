"""Cost-control contract for the advisory AI roles (reflection + analysis).

Chat and review cycles already meter every completion into ai_usage and pause
once the monthly budget is exhausted. These tests pin the same contract onto
the two advisory consumers: every reflection/analysis completion is metered
through an injected record_usage callable, both sweeps skip outright (no
completions) when the injected over_budget gate trips, and stored artifacts
record the model that actually generated the prose (the utility tier when
model tiering is on).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from poseidon.ai.analysis_service import AnalysisService
from poseidon.ai.reflection_service import ReflectionService
from poseidon.analytics.performance import FillRecord
from poseidon.core.config import AnalysisConfig, ReflectionConfig
from poseidon.core.enums import OrderSide
from poseidon.core.models import Quote
from poseidon.storage.db import Database

from .backend_fakes import FakeBackend, text_end


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def _over() -> bool:
    return True


async def _under() -> bool:
    return False


# ------------------------------------------------------------------ reflection


def _fills() -> list[FillRecord]:
    return [
        FillRecord(symbol="SPY", side=OrderSide.BUY, quantity=Decimal("10"),
                   price=Decimal("100"), at=datetime(2026, 6, 1, tzinfo=UTC),
                   strategy="mom", decision_id="d1"),
        FillRecord(symbol="SPY", side=OrderSide.SELL, quantity=Decimal("10"),
                   price=Decimal("96"), at=datetime(2026, 6, 4, tzinfo=UTC),
                   strategy="mom", decision_id="d1"),
    ]


class _Router:
    async def bars(self, symbol, *, timeframe="1d", limit=100):
        return []


def _reflection(db, *, backend, recorded, over_budget=_under, model="primary-model"):
    async def _load(symbol, since=None):
        return [f for f in _fills() if symbol is None or f.symbol == symbol]

    async def _audit(*a):
        return None

    async def _record(usage: dict[str, int]) -> None:
        recorded.append(usage)

    return ReflectionService(
        db=db, router=_Router(), config=ReflectionConfig(), model=model,
        get_backend=lambda: backend, load_fills=_load,
        is_flat=lambda s: True, audit_append=_audit,
        record_usage=_record, over_budget=over_budget)


async def test_reflection_completion_is_metered(db) -> None:
    recorded: list[dict[str, int]] = []
    svc = _reflection(db, backend=FakeBackend([text_end("lesson")]), recorded=recorded)
    await svc.reflect_episode("SPY")
    assert recorded == [{"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                         "cache_write_tokens": 0, "api_calls": 1}]


async def test_reflection_meters_usage_attached_to_a_failed_call(db) -> None:
    class _Boom:
        model = "boom"

        async def complete(self, *a: Any, **k: Any) -> Any:
            exc = RuntimeError("down")
            exc.usage = {"input_tokens": 7, "output_tokens": 0}  # type: ignore[attr-defined]
            raise exc

    recorded: list[dict[str, int]] = []
    svc = _reflection(db, backend=_Boom(), recorded=recorded)
    await svc.reflect_episode("SPY")  # must not raise, must not store a lesson
    assert await db.fetch_one("SELECT COUNT(*) FROM trade_lessons") == (0,)
    assert recorded and recorded[0]["input_tokens"] == 7
    assert recorded[0]["api_calls"] == 1


async def test_reflection_sweep_skips_when_over_budget(db) -> None:
    recorded: list[dict[str, int]] = []
    backend = FakeBackend([text_end("lesson")])
    svc = _reflection(db, backend=backend, recorded=recorded, over_budget=_over)
    await svc.on_account_synced("t", {})
    await asyncio.gather(*svc._tasks)
    assert backend.calls == []                # not a single completion
    assert recorded == []
    assert await db.fetch_one("SELECT COUNT(*) FROM trade_lessons") == (0,)


async def test_lesson_records_the_generating_backend_model(db) -> None:
    recorded: list[dict[str, int]] = []
    svc = _reflection(db, backend=FakeBackend([text_end("lesson")]),
                      recorded=recorded, model="primary-model")
    await svc.reflect_episode("SPY")
    row = await db.fetch_one("SELECT model FROM trade_lessons")
    assert row == ("fake",)                   # FakeBackend.model, not the primary


# -------------------------------------------------------------------- analysis


class _AnalysisRouter:
    async def quote(self, s, allow_delayed=True):
        return Quote(symbol="AAPL", last=Decimal("190.10"),
                     as_of=datetime.now(UTC), source="fake")

    async def bars(self, s, timeframe="1d", limit=30):
        return []


class _FirmBackend:
    """Scripted firm backend: stage-appropriate JSON with real per-call usage."""

    model = "utility-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        self.calls += 1
        if "facilitator" in system.lower():
            return text_end('{"direction":"long","conviction":0.6,"synthesis":"s"}')
        return text_end('{"stance":"bullish","confidence":0.6,"summary":"s",'
                        '"key_points":[],"data_gaps":[],"sources":[]}')


def _analysis(db, *, backend, recorded, over_budget=_under, model="primary-model"):
    async def _audit(*a):
        return None

    async def _record(usage: dict[str, int]) -> None:
        recorded.append(usage)

    return AnalysisService(
        db=db, router=_AnalysisRouter(),
        config=AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1),
        model=model, get_backend=lambda: backend, watchlist=lambda: ["AAPL"],
        audit_append=_audit, scan=None,
        record_usage=_record, over_budget=over_budget)


async def test_analyze_symbol_meters_every_completion(db) -> None:
    recorded: list[dict[str, int]] = []
    backend = _FirmBackend()
    svc = _analysis(db, backend=backend, recorded=recorded)
    await svc.analyze_symbol("AAPL")
    # 4 analysts + (bull+bear) + facilitator + 3 risk voices + synthesis = 11.
    assert backend.calls == 11
    assert len(recorded) == 1
    assert recorded[0]["api_calls"] == 11
    assert recorded[0]["input_tokens"] == 11  # 1 token per scripted completion


async def test_analysis_sweep_skips_when_over_budget(db) -> None:
    recorded: list[dict[str, int]] = []
    backend = _FirmBackend()
    svc = _analysis(db, backend=backend, recorded=recorded, over_budget=_over)
    await svc.run_sweep()
    await asyncio.gather(*svc._tasks)
    assert backend.calls == 0                 # no completions at all
    assert recorded == []
    assert await db.fetch_one("SELECT COUNT(*) FROM analysis_packets") == (0,)


async def test_packet_records_the_generating_backend_model(db) -> None:
    recorded: list[dict[str, int]] = []
    svc = _analysis(db, backend=_FirmBackend(), recorded=recorded, model="primary-model")
    await svc.analyze_symbol("AAPL")
    got = await db.recent_packets(["AAPL"], refresh_hours=24, limit=1, now=datetime.now(UTC))
    assert got and got[0].model == "utility-model"


# ------------------------------------------------------------- kernel wiring


async def test_wire_ai_binds_budget_gate_and_role_tagged_metering(tmp_path) -> None:
    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AIConfig, AppConfig
    from poseidon.security.vault import Vault

    executed: list[tuple] = []

    class _Db:
        async def execute(self, sql, params):
            executed.append(params)

    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    kernel.db = _Db()  # type: ignore[assignment]
    kernel.router = None  # type: ignore[assignment]
    kernel.audit = SimpleNamespace(append=None)  # type: ignore[assignment]
    cfg = AIConfig(backend="openai_compatible", base_url="http://x/v1",
                   model="big", utility_model="small")
    kernel._wire_ai(cfg, object(), object())  # type: ignore[arg-type]

    assert kernel.reflection is not None and kernel.analysis is not None
    # Both sweeps consult the kernel's month-to-date budget gate.
    assert kernel.reflection._over_budget == kernel._over_ai_budget
    assert kernel.analysis._over_budget == kernel._over_ai_budget
    # Metering lands in ai_usage with a role-tagged usage id.
    usage = {"input_tokens": 3, "output_tokens": 2, "api_calls": 2}
    assert kernel.reflection._record_usage is not None
    await kernel.reflection._record_usage(usage)
    assert kernel.analysis._record_usage is not None
    await kernel.analysis._record_usage(usage)
    ids = [str(p[0]) for p in executed]
    assert len(ids) == 2
    assert ids[0].startswith("reflection-") and ids[1].startswith("analysis-")
