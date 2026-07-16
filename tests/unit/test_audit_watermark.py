"""Regression tests for the reflection fill watermark.

First-run seeding: a database with pre-existing filled orders (upgrade path)
must not backfill-reflect its history — the watermark seeds to the newest
existing fill and lessons start from now. TOCTOU: a closing fill whose
position is not yet flat in the synced snapshot must be re-seen by the next
sweep, not consumed, so its lesson lands exactly once when the snapshot
catches up.
"""
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

_WATERMARK_KEY = "reflection.fill_watermark"


class _Router:
    async def bars(self, symbol, *, timeframe="1d", limit=100):
        return []  # no benchmark bars -> alpha None; reflection still proceeds


def _fill(side: OrderSide, at: datetime, symbol: str = "SPY") -> FillRecord:
    return FillRecord(symbol=symbol, side=side, quantity=Decimal("10"),
                      price=Decimal("100"), at=at, strategy="mom", decision_id="d1")


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def _service(db, *, backend: FakeBackend, fills: list[FillRecord],
             flat: dict[str, bool]) -> ReflectionService:
    async def _load(symbol, since=None):
        out = [f for f in fills if symbol is None or f.symbol == symbol]
        if since:
            out = [f for f in out if f.at.isoformat() > since]
        return out

    async def _audit(actor, action, payload):
        return None

    return ReflectionService(
        db=db, router=_Router(), config=ReflectionConfig(), model="fake",
        get_backend=lambda: backend, load_fills=_load,
        is_flat=lambda s: flat.get(s, True), audit_append=_audit)


async def _drain(svc: ReflectionService) -> None:
    await asyncio.gather(*svc._tasks)


async def _lesson_count(db, symbol="SPY") -> int:
    rows = await db.fetch_all("SELECT id FROM trade_lessons WHERE symbol = ?", (symbol,))
    return len(rows)


async def test_first_sweep_seeds_watermark_past_history_without_reflecting(db) -> None:
    # Upgrade path: months of pre-existing fills, no watermark yet.
    history = [
        _fill(OrderSide.BUY, datetime(2026, 2, 2, tzinfo=UTC)),
        _fill(OrderSide.SELL, datetime(2026, 2, 9, tzinfo=UTC)),
        _fill(OrderSide.BUY, datetime(2026, 5, 1, tzinfo=UTC), symbol="AAPL"),
        _fill(OrderSide.SELL, datetime(2026, 5, 6, tzinfo=UTC), symbol="AAPL"),
    ]
    backend = FakeBackend([text_end("stale lesson"), text_end("stale lesson")])
    svc = _service(db, backend=backend, fills=history, flat={})
    await svc.on_account_synced("t", {})
    await _drain(svc)
    assert backend.calls == []  # zero reflection LLM calls on history
    assert await _lesson_count(db, "SPY") == 0
    assert await _lesson_count(db, "AAPL") == 0
    # Watermark advanced to the newest existing fill: lessons start from now.
    assert await db.kv_get(_WATERMARK_KEY, "") == "2026-05-06T00:00:00+00:00"


async def test_first_sweep_on_empty_book_seeds_watermark_to_now(db) -> None:
    svc = _service(db, backend=FakeBackend([]), fills=[], flat={})
    await svc.on_account_synced("t", {})
    await _drain(svc)
    seed = await db.kv_get(_WATERMARK_KEY, "")
    assert seed != ""  # initialized, so the first real close is swept later
    assert datetime.fromisoformat(seed).tzinfo is not None


async def test_close_after_seeding_is_still_reflected(db) -> None:
    fills: list[FillRecord] = []
    backend = FakeBackend([text_end("fresh lesson")])
    svc = _service(db, backend=backend, fills=fills, flat={})
    await svc.on_account_synced("t", {})  # seeds only
    await _drain(svc)
    seed = await db.kv_get(_WATERMARK_KEY, "")
    fills += [_fill(OrderSide.BUY, datetime(2099, 1, 2, tzinfo=UTC)),
              _fill(OrderSide.SELL, datetime(2099, 1, 5, tzinfo=UTC))]
    assert all(f.at.isoformat() > seed for f in fills)
    await svc.on_account_synced("t", {})
    await _drain(svc)
    assert await _lesson_count(db) == 1


async def test_stale_snapshot_close_is_requeued_and_reflected_exactly_once(db) -> None:
    # TOCTOU: the final close is persisted, but the synced portfolio snapshot
    # was fetched before it and still shows the position open.
    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    fills = [_fill(OrderSide.BUY, datetime(2026, 6, 2, tzinfo=UTC)),
             _fill(OrderSide.SELL, datetime(2026, 6, 3, tzinfo=UTC))]
    flat = {"SPY": False}
    backend = FakeBackend([text_end("lesson"), text_end("lesson")])
    svc = _service(db, backend=backend, fills=fills, flat=flat)
    await db.kv_set(_WATERMARK_KEY, t0.isoformat())  # already initialized

    await svc.on_account_synced("t", {})  # stale snapshot: SPY not flat yet
    await _drain(svc)
    assert await _lesson_count(db) == 0
    # The unresolved close must not be consumed: the watermark may advance up
    # to (but not past) it, so the next sweep re-sees the SELL.
    assert await db.kv_get(_WATERMARK_KEY, "") < fills[1].at.isoformat()

    flat["SPY"] = True  # next sync: snapshot caught up, position flat
    await svc.on_account_synced("t", {})
    await _drain(svc)
    assert await _lesson_count(db) == 1
    assert await db.kv_get(_WATERMARK_KEY, "") == fills[1].at.isoformat()

    await svc.on_account_synced("t", {})  # exactly once: dedup on re-sweep
    await _drain(svc)
    assert await _lesson_count(db) == 1
