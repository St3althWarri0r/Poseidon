from __future__ import annotations

import asyncio
import json
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


# Sweeps below pre-seed the watermark: an empty watermark means first run,
# which only seeds past existing fills (see test_audit_watermark.py).
_SEED = "2026-05-01T00:00:00+00:00"


async def test_on_account_synced_reflects_flat_symbol_and_advances_watermark(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("lesson")]), fills=_fills())
    await db.kv_set("reflection.fill_watermark", _SEED)
    await svc.on_account_synced("t", {})
    await asyncio.gather(*svc._tasks)  # drain the background reflection task
    assert await _lesson_count(db) == 1
    assert await db.kv_get("reflection.fill_watermark", "") > _SEED


async def test_first_sweep_seeds_watermark_instead_of_reflecting(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("lesson")]), fills=_fills())
    await svc.on_account_synced("t", {})  # no watermark yet: upgrade/first-run path
    await asyncio.gather(*svc._tasks)
    assert await _lesson_count(db) == 0
    assert await db.kv_get("reflection.fill_watermark", "") == "2026-06-04T00:00:00+00:00"


async def test_on_account_synced_skips_when_not_flat(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("lesson")]), fills=_fills(), is_flat=False)
    await db.kv_set("reflection.fill_watermark", _SEED)
    await svc.on_account_synced("t", {})
    await asyncio.gather(*svc._tasks)
    assert await _lesson_count(db) == 0


async def test_sweep_load_is_bounded_by_watermark(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("l1"), text_end("l2")]), fills=_fills())
    await db.kv_set("reflection.fill_watermark", _SEED)
    await svc.on_account_synced("t", {})
    await asyncio.gather(*svc._tasks)
    await svc.on_account_synced("t", {})  # second sync
    await asyncio.gather(*svc._tasks)
    sweep_sinces = [since for (sym, since) in svc.load_calls if sym is None]  # type: ignore[attr-defined]
    assert sweep_sinces[0] == _SEED           # first sweep: bounded by the seed
    assert sweep_sinces[1] > _SEED            # second sweep: bounded by advanced watermark
    assert await _lesson_count(db) == 1       # dedup + bound -> still one lesson


async def test_relevant_lessons_respects_inject_flag(db) -> None:
    svc = _service(db, backend=FakeBackend([text_end("lesson")]), fills=_fills())
    await svc.reflect_episode("SPY")
    assert len(await svc.relevant_lessons(["SPY"])) == 1
    off = _service(db, backend=FakeBackend([]), fills=_fills(),
                   cfg=ReflectionConfig(inject=False))
    assert await off.relevant_lessons(["SPY"]) == []


async def test_reflection_prompt_carries_entry_conviction_and_invalidation(db) -> None:
    # The stored entry decision's confidence + invalidation must reach the
    # reflection prompt so lessons can score whether the conviction was earned.
    payload = json.dumps({"rationale": {
        "thesis": "momentum breakout", "confidence": 0.85,
        "invalidation": "loses the 50dma on volume"}})
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("d1", "c1", "buy", payload, "2026-06-01T00:00:00+00:00"))
    backend = FakeBackend([text_end("Conviction was justified.")])
    svc = _service(db, backend=backend, fills=_fills())
    await svc.reflect_episode("SPY")
    assert await _lesson_count(db) == 1
    sent = backend.calls[0]["messages"][0]["content"]
    # Line-anchored: a thesis<->invalidation tuple swap keeps both substrings
    # present somewhere; anchoring to the labeled lines kills that mutant.
    assert "Original entry thesis: momentum breakout" in sent
    assert "Entry conviction: 85%." in sent
    assert "Stated invalidation: loses the 50dma on volume" in sent


async def test_legacy_decision_without_risk_case_still_reflects(db) -> None:
    # Rationale JSON from before the fields existed: no crash, no noise lines.
    payload = json.dumps({"rationale": {"thesis": "old style"}})
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("d1", "c1", "buy", payload, "2026-06-01T00:00:00+00:00"))
    backend = FakeBackend([text_end("Lesson.")])
    svc = _service(db, backend=backend, fills=_fills())
    await svc.reflect_episode("SPY")
    assert await _lesson_count(db) == 1
    sent = backend.calls[0]["messages"][0]["content"]
    assert "old style" in sent
    assert "conviction" not in sent.lower()


async def test_out_of_range_confidence_clamps_instead_of_killing(db) -> None:
    # A junk row (confidence 7) must clamp into the model's [0,1] bounds —
    # dropping the clamp turns it into a ValidationError swallowed by the
    # best-effort except, and the lesson is permanently lost (the watermark
    # has already advanced past the close).
    payload = json.dumps({"rationale": {"thesis": "t", "confidence": 7}})
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("d1", "c1", "buy", payload, "2026-06-01T00:00:00+00:00"))
    backend = FakeBackend([text_end("Lesson.")])
    svc = _service(db, backend=backend, fills=_fills())
    await svc.reflect_episode("SPY")
    assert await _lesson_count(db) == 1
    assert "Entry conviction: 100%." in backend.calls[0]["messages"][0]["content"]


async def test_nan_confidence_degrades_without_killing_episode(db) -> None:
    # NaN passes isinstance and slides through min/max (every comparison is
    # False) — it must be filtered out, not allowed to fail the episode.
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("d1", "c1", "buy", '{"rationale": {"thesis": "t", "confidence": NaN}}',
         "2026-06-01T00:00:00+00:00"))
    backend = FakeBackend([text_end("Lesson.")])
    svc = _service(db, backend=backend, fills=_fills())
    await svc.reflect_episode("SPY")
    assert await _lesson_count(db) == 1
    assert "conviction" not in backend.calls[0]["messages"][0]["content"].lower()


async def test_bool_confidence_is_junk_not_full_conviction(db) -> None:
    # bool subclasses int; "confidence": true must not fabricate an
    # "Entry conviction: 100%." line for the lesson writer to judge.
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("d1", "c1", "buy", '{"rationale": {"thesis": "t", "confidence": true}}',
         "2026-06-01T00:00:00+00:00"))
    backend = FakeBackend([text_end("Lesson.")])
    svc = _service(db, backend=backend, fills=_fills())
    await svc.reflect_episode("SPY")
    assert await _lesson_count(db) == 1
    assert "conviction" not in backend.calls[0]["messages"][0]["content"].lower()
