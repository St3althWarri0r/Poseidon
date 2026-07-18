"""Shutdown drain contract for the advisory background services.

ReflectionService and AnalysisService spawn fire-and-forget LLM tasks that
write to the DB. At kernel stop those tasks must be drained — a short grace
window so near-complete work lands its write, then cancellation — BEFORE the
backends, router, and DB are closed underneath them. Otherwise a completed
(billed) reflection loses its lesson to a closed DB, and because the fill
watermark has already advanced past that close, the loss is permanent. A
stopped service must also refuse late sweeps (a bus-drained in-flight
on_account_synced) so they cannot spawn doomed tasks or advance the watermark.
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
from poseidon.app import ApplicationKernel
from poseidon.core.config import AnalysisConfig, AppConfig, ReflectionConfig
from poseidon.core.enums import OrderSide
from poseidon.core.models import Quote
from poseidon.security.vault import Vault
from poseidon.storage.db import Database

from .backend_fakes import FakeBackend, text_end

_WATERMARK_KEY = "reflection.fill_watermark"
_SEED = "2026-05-01T00:00:00+00:00"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


class _HungBackend:
    """Blocks inside complete() forever — an in-flight LLM call at shutdown."""

    model = "hung"

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        self.entered.set()
        await asyncio.Event().wait()

    def tool_result_messages(self, results):
        return []

    async def aclose(self):
        return None


# ------------------------------------------------------------------ reflection


class _BarsRouter:
    async def bars(self, symbol, *, timeframe="1d", limit=100):
        return []


def _fills() -> list[FillRecord]:
    return [
        FillRecord(symbol="SPY", side=OrderSide.BUY, quantity=Decimal("10"),
                   price=Decimal("100"), at=datetime(2026, 6, 1, tzinfo=UTC),
                   strategy="mom", decision_id="d1"),
        FillRecord(symbol="SPY", side=OrderSide.SELL, quantity=Decimal("10"),
                   price=Decimal("96"), at=datetime(2026, 6, 4, tzinfo=UTC),
                   strategy="mom", decision_id="d1"),
    ]


def _reflection(db: Database, *, backend: Any) -> ReflectionService:
    fills = _fills()

    async def _load(symbol, since=None):
        out = [f for f in fills if symbol is None or f.symbol == symbol]
        if since:
            out = [f for f in out if f.at.isoformat() > since]
        return out

    async def _audit(actor, action, payload):
        return None

    return ReflectionService(
        db=db, router=_BarsRouter(), config=ReflectionConfig(), model="fake",
        get_backend=lambda: backend, load_fills=_load,
        is_flat=lambda s: True, audit_append=_audit)


async def _lesson_count(db: Database) -> int:
    rows = await db.fetch_all("SELECT id FROM trade_lessons WHERE symbol = ?", ("SPY",))
    return len(rows)


async def test_reflection_stop_cancels_hung_task_and_completes(db) -> None:
    backend = _HungBackend()
    svc = _reflection(db, backend=backend)
    await db.kv_set(_WATERMARK_KEY, _SEED)
    await svc.on_account_synced("t", {})
    await asyncio.wait_for(backend.entered.wait(), 2)   # task is mid-LLM-call
    task = next(iter(svc._tasks))
    await asyncio.wait_for(svc.stop(grace_seconds=0.05), 2)  # must not hang
    assert task.cancelled()
    assert not svc._tasks


async def test_reflection_stop_lets_fast_task_land_lesson_before_close(db) -> None:
    svc = _reflection(db, backend=FakeBackend([text_end("lesson prose")]))
    await db.kv_set(_WATERMARK_KEY, _SEED)
    await svc.on_account_synced("t", {})
    await svc.stop()
    # The write landed before stop() returned — i.e. before the kernel would
    # go on to close the backend and DB.
    assert await _lesson_count(db) == 1


async def test_reflection_sweep_is_noop_after_stop(db) -> None:
    # A late sweep (an in-flight handler drained by bus.close after the
    # service stopped) must neither spawn tasks against the closing backend
    # nor advance the watermark past closes whose reflection never ran.
    svc = _reflection(db, backend=FakeBackend([text_end("lesson prose")]))
    await db.kv_set(_WATERMARK_KEY, _SEED)
    await svc.stop()
    await svc.on_account_synced("t", {})
    assert not svc._tasks
    assert await db.kv_get(_WATERMARK_KEY, "") == _SEED


# ------------------------------------------------------------------- analysis


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "m"


class _ScriptedBackend:
    model = "m"

    async def complete(self, messages, *, tools, system, force_tool=None, max_tokens=None):
        if "facilitator" in system.lower():
            return _Resp('{"direction":"long","conviction":0.6,"synthesis":"s"}')
        return _Resp('{"stance":"bullish","confidence":0.6,"summary":"s",'
                     '"key_points":[],"data_gaps":[],"sources":[]}')


class _QuoteRouter:
    async def quote(self, s, allow_delayed=True):
        return Quote(symbol="AAPL", last=Decimal("190.10"),
                     as_of=datetime.now(UTC), source="fake")

    async def bars(self, s, timeframe="1d", limit=30):
        return []


def _analysis(db: Database, *, backend: Any) -> AnalysisService:
    async def _audit(actor, action, payload):
        return None

    cfg = AnalysisConfig(enabled=True, debate_rounds=1, risk_rounds=1)
    return AnalysisService(db=db, router=_QuoteRouter(), config=cfg, model="m",
                           get_backend=lambda: backend, watchlist=lambda: ["AAPL"],
                           audit_append=_audit, scan=None)


async def test_analysis_stop_cancels_hung_task_and_completes(db) -> None:
    backend = _HungBackend()
    svc = _analysis(db, backend=backend)
    await svc.run_sweep()
    await asyncio.wait_for(backend.entered.wait(), 2)   # pipeline is mid-LLM-call
    task = next(iter(svc._tasks))
    await asyncio.wait_for(svc.stop(grace_seconds=0.05), 2)  # must not hang
    assert task.cancelled()
    assert not svc._tasks


async def test_analysis_stop_lets_fast_task_land_packet_before_close(db) -> None:
    svc = _analysis(db, backend=_ScriptedBackend())
    await svc.run_sweep()
    await svc.stop()
    row = await db.fetch_one("SELECT COUNT(*) FROM analysis_packets")
    assert row is not None and row[0] == 1


async def test_analysis_sweep_is_noop_after_stop(db) -> None:
    svc = _analysis(db, backend=_ScriptedBackend())
    await svc.stop()
    await svc.run_sweep()
    assert not svc._tasks


# --------------------------------------------------------------------- kernel


async def test_kernel_stop_drains_advisory_services_before_closing_deps(tmp_path) -> None:
    calls: list[str] = []

    def rec(name: str):
        async def _f(*a: Any, **k: Any) -> None:
            calls.append(name)
        return _f

    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    kernel.audit = SimpleNamespace(append=rec("audit.append"))  # type: ignore[assignment]
    for attr in ("dashboard", "updates", "health", "scheduler", "sync"):
        setattr(kernel, attr, SimpleNamespace(stop=rec(f"{attr}.stop")))
    kernel.guardian = SimpleNamespace(drain=rec("guardian.drain"))  # type: ignore[assignment]
    kernel.order_manager = SimpleNamespace(stop=rec("order_manager.stop"))  # type: ignore[assignment]
    kernel.broker = SimpleNamespace(disconnect=rec("broker.disconnect"))  # type: ignore[assignment]
    kernel.router = SimpleNamespace(close=rec("router.close"))  # type: ignore[assignment]
    kernel.db = SimpleNamespace(close=rec("db.close"))  # type: ignore[assignment]
    kernel.reflection = SimpleNamespace(stop=rec("reflection.stop"))  # type: ignore[assignment]
    kernel.analysis = SimpleNamespace(stop=rec("analysis.stop"))  # type: ignore[assignment]

    await kernel.stop()

    # Both advisory services are drained, and strictly before the router,
    # broker, and DB their in-flight tasks write to are closed.
    for svc_stop in ("reflection.stop", "analysis.stop"):
        assert svc_stop in calls
        for closed in ("broker.disconnect", "router.close", "db.close"):
            assert calls.index(svc_stop) < calls.index(closed)
