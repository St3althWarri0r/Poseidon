"""Regression tests for storage migration safety and advisory retention.

Covers: (a) opening a pre-v2.4.0 database (orders table without account_scope)
through the real Database.open() path — the scope/status index must not run
before the column migration; (b) pruning of advisory trade_lessons and
analysis_packets plus the index backing the global recent-lessons query.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poseidon.core.models import (
    AnalysisPacket,
    AnalystReport,
    DebateVerdict,
    RiskLens,
    TradeLesson,
)
from poseidon.storage.db import Database

_PRE_V240_ORDERS = """
CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    broker TEXT NOT NULL,
    broker_order_id TEXT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_created ON orders(created_at);
"""

# decisions + trade_lessons as they shipped BEFORE the outcome-resolution
# columns (decisions.resolved_at/resolution, trade_lessons.kind) existed.
_PRE_OUTCOME_DDL = """
CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_decisions_created ON decisions(created_at);
CREATE TABLE trade_lessons (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT '',
    decision_id TEXT,
    entered_at TEXT NOT NULL,
    exited_at TEXT NOT NULL,
    realized_return REAL NOT NULL,
    alpha REAL,
    holding_days REAL NOT NULL,
    lesson TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _make_pre_v240_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_V240_ORDERS)
    conn.execute(
        "INSERT INTO orders (id, client_order_id, broker, payload, status, "
        "created_at, updated_at) VALUES ('o1', 'c1', 'paper', '{}', 'filled', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def _lesson(symbol: str, *, created_at: datetime) -> TradeLesson:
    return TradeLesson(
        id=f"{symbol}-{created_at.isoformat()}", symbol=symbol, strategy="s",
        decision_id="d1", entered_at=created_at - timedelta(days=3),
        exited_at=created_at, realized_return=0.01, alpha=0.005,
        holding_days=3.0, lesson=f"lesson for {symbol}", model="fake",
        created_at=created_at)


def _packet(symbol: str, *, as_of: datetime) -> AnalysisPacket:
    return AnalysisPacket(
        id=f"{symbol}-{as_of.isoformat()}", symbol=symbol, as_of=as_of, model="m",
        reports=[AnalystReport(role="news", summary="s", stance="neutral",
                               confidence=0.5, key_points=[], data_gaps=[], sources=[])],
        verdict=DebateVerdict(direction="avoid", conviction=0.4, bull_case="b",
                              bear_case="c", synthesis="s", rounds=1),
        risk_lens=RiskLens(aggressive="a", neutral="n", conservative="c", synthesis="s"),
        snapshot_digest="d")


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_open_migrates_pre_v240_orders_table(tmp_path) -> None:
    """A pre-account_scope database must open cleanly through Database.open()."""
    path = tmp_path / "legacy.db"
    _make_pre_v240_db(path)
    db = Database(path)
    await db.open()  # must not raise 'no such column: account_scope'
    try:
        row = await db.fetch_one("SELECT account_scope FROM orders WHERE id = 'o1'")
        assert row == ("",)  # legacy rows get the '' scope from the migration
        idx = await db.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_orders_scope_status_updated'")
        assert idx is not None  # index still lands once the column exists
    finally:
        await db.close()


async def test_fresh_db_still_gets_scope_index(db: Database) -> None:
    idx = await db.fetch_one(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_orders_scope_status_updated'")
    assert idx is not None


async def test_global_recent_lessons_uses_created_at_index(db: Database) -> None:
    """The cross-ticker arm of recent_lessons must not full-scan trade_lessons."""
    rows = await db.fetch_all(
        "EXPLAIN QUERY PLAN SELECT * FROM trade_lessons WHERE created_at >= ? "
        "ORDER BY created_at DESC LIMIT ?",
        ("2026-01-01", 3))
    plan = " ".join(str(r[3]) for r in rows)
    assert "idx_trade_lessons_created" in plan
    assert "SCAN trade_lessons" not in plan


async def test_prune_advisory_deletes_only_expired_rows(db: Database) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    lookback_days, refresh_hours = 30, 24
    old_lesson = _lesson("OLD", created_at=now - timedelta(days=2 * lookback_days + 1))
    new_lesson = _lesson("SPY", created_at=now - timedelta(days=5))
    await db.add_trade_lesson(old_lesson)
    await db.add_trade_lesson(new_lesson)
    await db.add_analysis_packet(_packet("OLD", as_of=now - timedelta(hours=2 * refresh_hours + 1)))
    await db.add_analysis_packet(_packet("SPY", as_of=now - timedelta(hours=2)))

    lessons_deleted, packets_deleted = await db.prune_advisory(
        lesson_lookback_days=lookback_days, packet_refresh_hours=refresh_hours, now=now)

    assert (lessons_deleted, packets_deleted) == (1, 1)
    assert await db.fetch_all("SELECT symbol FROM trade_lessons") == [("SPY",)]
    assert await db.fetch_all("SELECT symbol FROM analysis_packets") == [("SPY",)]
    # Kept rows stay readable through the normal advisory readers.
    kept = await db.recent_lessons(
        ["SPY"], per_symbol=2, global_n=2, lookback_days=lookback_days, limit=8, now=now)
    assert [ls.symbol for ls in kept] == ["SPY"]
    packets = await db.recent_packets(["SPY"], refresh_hours=refresh_hours, limit=3, now=now)
    assert [p.symbol for p in packets] == ["SPY"]


async def _columns(db: Database, table: str) -> list[str]:
    rows = await db.fetch_all(f"PRAGMA table_info({table})")
    return [str(r[1]) for r in rows]


async def test_open_migrates_pre_outcome_decisions_and_lessons(tmp_path) -> None:
    """A pre-outcome-resolution DB gains resolved_at/resolution and kind on
    open(), with column order IDENTICAL to a fresh DB — recent_lessons reads
    trade_lessons positionally via SELECT *, so a divergent order on either
    path silently corrupts every lesson-consuming surface."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_OUTCOME_DDL)
    conn.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES ('d-old', 'c1', 'trade', '{}', '2026-01-01T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO trade_lessons (id, symbol, strategy, decision_id, entered_at, "
        "exited_at, realized_return, alpha, holding_days, lesson, model, created_at) "
        "VALUES ('l-old', 'SPY', 's', 'd-old', '2026-01-01T00:00:00+00:00', "
        "'2026-01-03T00:00:00+00:00', 0.01, 0.005, 2.0, 'old lesson', 'm', "
        "'2026-01-03T00:00:00+00:00')")
    conn.commit()
    conn.close()

    legacy = Database(path)
    await legacy.open()
    fresh = Database(tmp_path / "fresh.db")
    await fresh.open()
    try:
        for table in ("decisions", "trade_lessons"):
            assert await _columns(legacy, table) == await _columns(fresh, table)
        assert (await _columns(legacy, "decisions"))[-2:] == ["resolved_at", "resolution"]
        assert (await _columns(legacy, "trade_lessons"))[-1] == "kind"
        # Old decisions read back PENDING; old lessons backfill kind='trade'.
        row = await legacy.fetch_one(
            "SELECT resolved_at, resolution FROM decisions WHERE id = 'd-old'")
        assert row == (None, None)
        row = await legacy.fetch_one("SELECT kind FROM trade_lessons WHERE id = 'l-old'")
        assert row == ("trade",)
        kept = await legacy.recent_lessons(
            ["SPY"], per_symbol=2, global_n=2, lookback_days=365, limit=8,
            now=datetime(2026, 2, 1, tzinfo=UTC))
        assert [(ls.id, ls.kind) for ls in kept] == [("l-old", "trade")]
    finally:
        await legacy.close()
        await fresh.close()


async def _decision(db: Database, did: str, created_at: str, *,
                    action: str = "trade") -> None:
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (did, "c1", action, "{}", created_at))


async def test_unresolved_decisions_filters_order_and_limit(db: Database) -> None:
    await _decision(db, "d1", "2026-06-01T00:00:00+00:00")
    await _decision(db, "d2", "2026-06-02T00:00:00+00:00")
    await _decision(db, "d3", "2026-06-03T00:00:00+00:00")
    await _decision(db, "d4", "2026-06-10T00:00:00+00:00")  # after `before` cutoff
    await _decision(db, "d0", "2026-05-01T00:00:00+00:00")  # at/before `after` cutoff
    await db.mark_decision_resolved(
        "d2", resolved_at_iso="2026-06-09T00:00:00+00:00",
        resolution_json='{"status": "executed"}')

    rows = await db.unresolved_decisions(
        after="2026-05-01T00:00:00+00:00", before="2026-06-05T00:00:00+00:00", limit=10)
    assert [r[0] for r in rows] == ["d1", "d3"]  # resolved + cutoffs excluded, ASC
    assert rows[0] == ("d1", "trade", "{}", "2026-06-01T00:00:00+00:00")

    capped = await db.unresolved_decisions(
        after="2026-05-01T00:00:00+00:00", before="2026-06-05T00:00:00+00:00", limit=1)
    assert [r[0] for r in capped] == ["d1"]


async def test_mark_decision_resolved_round_trips(db: Database) -> None:
    await _decision(db, "d1", "2026-06-01T00:00:00+00:00")
    await db.mark_decision_resolved(
        "d1", resolved_at_iso="2026-06-09T00:00:00+00:00",
        resolution_json='{"status": "unexecuted", "horizon_trading_days": 5}')
    row = await db.fetch_one(
        "SELECT resolved_at, resolution FROM decisions WHERE id = 'd1'")
    assert row is not None and row[0] == "2026-06-09T00:00:00+00:00"
    assert row[1] == '{"status": "unexecuted", "horizon_trading_days": 5}'


async def test_prune_advisory_never_touches_reader_windows(db: Database) -> None:
    """Rows still visible to recent_lessons/recent_packets must survive a prune."""
    now = datetime(2026, 7, 1, tzinfo=UTC)
    # Exactly at the reader cutoff: oldest row recent_lessons can still return.
    edge = _lesson("EDGE", created_at=now - timedelta(days=30))
    await db.add_trade_lesson(edge)
    await db.add_analysis_packet(_packet("EDGE", as_of=now - timedelta(hours=24)))
    await db.prune_advisory(lesson_lookback_days=30, packet_refresh_hours=24, now=now)
    kept = await db.recent_lessons(
        ["EDGE"], per_symbol=1, global_n=1, lookback_days=30, limit=8, now=now)
    assert [ls.symbol for ls in kept] == ["EDGE"]
    packets = await db.recent_packets(["EDGE"], refresh_hours=24, limit=3, now=now)
    assert [p.symbol for p in packets] == ["EDGE"]
