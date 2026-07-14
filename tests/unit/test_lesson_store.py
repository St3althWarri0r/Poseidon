from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from poseidon.core.models import TradeLesson
from poseidon.storage.db import Database


def _lesson(symbol: str, *, day: int, ret: float = 0.01) -> TradeLesson:
    t = datetime(2026, 6, day, tzinfo=UTC)
    return TradeLesson(
        id=f"{symbol}-{day}", symbol=symbol, strategy="s", decision_id="d1",
        entered_at=t - timedelta(days=3), exited_at=t, realized_return=ret,
        alpha=0.005, holding_days=3.0, lesson=f"lesson for {symbol} d{day}",
        model="fake", created_at=t)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_append_and_dedup(db: Database) -> None:
    lsn = _lesson("SPY", day=10)
    await db.add_trade_lesson(lsn)
    assert await db.lesson_exists("SPY", lsn.entered_at, lsn.exited_at) is True
    assert await db.lesson_exists("SPY", lsn.entered_at, lsn.exited_at + timedelta(days=1)) is False


async def test_recent_relevant_caps_and_scopes(db: Database) -> None:
    now = datetime(2026, 6, 20, tzinfo=UTC)
    for day in (5, 12, 18):
        await db.add_trade_lesson(_lesson("SPY", day=day))
    await db.add_trade_lesson(_lesson("QQQ", day=17))
    await db.add_trade_lesson(_lesson("IWM", day=1))  # older than lookback below
    out = await db.recent_lessons(
        ["SPY"], per_symbol=2, global_n=2, lookback_days=10, limit=8, now=now)
    syms = [l.symbol for l in out]
    assert syms.count("SPY") == 2          # per_symbol cap, most-recent first
    assert "QQQ" in syms                    # recent global reaches the cross-ticker lesson
    assert "IWM" not in syms                # dropped by lookback_days
    assert out[0].exited_at >= out[1].exited_at  # newest first


async def test_limit_is_hard_cap(db: Database) -> None:
    now = datetime(2026, 6, 20, tzinfo=UTC)
    for day in (11, 12, 13, 14, 15):
        await db.add_trade_lesson(_lesson("SPY", day=day))
    out = await db.recent_lessons(
        ["SPY"], per_symbol=10, global_n=10, lookback_days=60, limit=3, now=now)
    assert len(out) == 3
