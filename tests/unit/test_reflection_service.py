from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poseidon.ai.reflection_service import ReflectionService
from poseidon.analytics.performance import FillRecord
from poseidon.core.config import ReflectionConfig
from poseidon.core.enums import OrderSide
from poseidon.storage.db import Database

from .backend_fakes import FakeBackend, text_end


class _Router:
    async def bars(self, symbol, *, timeframe="1d", limit=100):
        return []  # no benchmark bars -> alpha None; reflection still proceeds


def _fills():
    return [
        FillRecord(symbol="SPY", side=OrderSide.BUY, quantity=Decimal("10"),
                   price=Decimal("100"), at=datetime(2026, 6, 1, tzinfo=UTC),
                   strategy="mom", decision_id="d1"),
        FillRecord(symbol="SPY", side=OrderSide.SELL, quantity=Decimal("10"),
                   price=Decimal("96"), at=datetime(2026, 6, 4, tzinfo=UTC),
                   strategy="mom", decision_id="d1"),
    ]


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def _service(db, *, backend, fills, is_flat=True, cfg=None):
    audited: list[tuple] = []
    load_calls: list[tuple] = []

    async def _load(symbol, since=None):
        load_calls.append((symbol, since))
        out = [f for f in fills if symbol is None or f.symbol == symbol]
        if since:
            out = [f for f in out if f.at.isoformat() > since]
        return out

    async def _audit(actor, action, payload):
        audited.append((actor, action, payload))

    svc = ReflectionService(
        db=db, router=_Router(), config=cfg or ReflectionConfig(), model="fake",
        get_backend=lambda: backend, load_fills=_load,
        is_flat=lambda s: is_flat, audit_append=_audit)
    svc.audited = audited  # type: ignore[attr-defined]
    svc.load_calls = load_calls  # type: ignore[attr-defined]
    return svc


async def _lesson_count(db, symbol="SPY") -> int:
    rows = await db.fetch_all("SELECT id FROM trade_lessons WHERE symbol = ?", (symbol,))
    return len(rows)


async def test_reflect_episode_stores_lesson_and_audits(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("Momentum failed; don't chase.")]), fills=_fills())
    await svc.reflect_episode("SPY")
    assert await _lesson_count(db) == 1
    assert svc.audited[0][1] == "lesson_written"  # type: ignore[attr-defined]


async def test_reflect_episode_dedups(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("a"), text_end("b")]), fills=_fills())
    await svc.reflect_episode("SPY")
    await svc.reflect_episode("SPY")  # same episode -> no second lesson
    assert await _lesson_count(db) == 1


async def test_reflect_episode_fail_open_on_backend_error(db) -> None:
    class Boom:
        model = "boom"
        async def complete(self, *a, **k):
            raise RuntimeError("down")
        def tool_result_messages(self, r):
            return []
        async def aclose(self):
            return None

    svc = _service(db, backend=Boom(), fills=_fills())
    await svc.reflect_episode("SPY")  # must not raise
    assert await _lesson_count(db) == 0


async def test_on_account_synced_reflects_flat_symbol_and_advances_watermark(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("lesson")]), fills=_fills())
    await svc.on_account_synced("t", {})
    await asyncio.gather(*svc._tasks)  # drain the background reflection task
    assert await _lesson_count(db) == 1
    assert await db.kv_get("reflection.fill_watermark", "") != ""


async def test_on_account_synced_skips_when_not_flat(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("lesson")]), fills=_fills(), is_flat=False)
    await svc.on_account_synced("t", {})
    await asyncio.gather(*svc._tasks)
    assert await _lesson_count(db) == 0


async def test_sweep_load_is_bounded_by_watermark(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("l1"), text_end("l2")]), fills=_fills())
    await svc.on_account_synced("t", {})
    await asyncio.gather(*svc._tasks)
    await svc.on_account_synced("t", {})  # second sync
    await asyncio.gather(*svc._tasks)
    sweep_sinces = [since for (sym, since) in svc.load_calls if sym is None]  # type: ignore[attr-defined]
    assert sweep_sinces[0] is None            # first sweep: no watermark yet
    assert sweep_sinces[1] not in (None, "")  # second sweep: SQL-bounded by watermark
    assert await _lesson_count(db) == 1       # dedup + bound -> still one lesson


async def test_relevant_lessons_respects_inject_flag(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("lesson")]), fills=_fills())
    await svc.reflect_episode("SPY")
    assert len(await svc.relevant_lessons(["SPY"])) == 1
    off = _service(db, backend=FakeBackend([]), fills=_fills(),
                   cfg=ReflectionConfig(inject=False))
    assert await off.relevant_lessons(["SPY"]) == []
