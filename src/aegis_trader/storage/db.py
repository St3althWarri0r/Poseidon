"""Async SQLite store.

One database file under the data directory holds durable runtime state:
orders, fills, AI decisions, approvals, audit chain, equity marks, and a
small key-value table for crash recovery. WAL mode keeps the dashboard's
reads from blocking the trading path.

Filesystem-level encryption of the data directory is documented in
docs/security.md (the vault covers credentials; position/order history is
protected by directory permissions plus optional fscrypt/LUKS).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    broker TEXT NOT NULL,
    broker_order_id TEXT,
    payload TEXT NOT NULL,          -- full Order JSON
    status TEXT NOT NULL,
    decision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,          -- full Decision JSON incl. rationale
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);

CREATE TABLE IF NOT EXISTS equity_marks (
    at TEXT PRIMARY KEY,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    day_pnl TEXT
);

CREATE TABLE IF NOT EXISTS exit_plans (
    symbol TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    stop_loss TEXT,
    take_profit TEXT,
    time_stop TEXT,
    quantity TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    triggered_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_usage (
    cycle_id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    api_calls INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_at ON ai_usage(at);

CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        # New databases must not be world readable.
        self._path.chmod(0o600)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = self.conn
        try:
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        await self.conn.execute(sql, tuple(params))
        await self.conn.commit()

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        cursor = await self.conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        return [tuple(r) for r in rows]

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> tuple[Any, ...] | None:
        cursor = await self.conn.execute(sql, tuple(params))
        row = await cursor.fetchone()
        return tuple(row) if row else None

    # -- kv helpers (crash recovery / small state) ---------------------------

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str)),
        )

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetch_one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row[0]) if row else default
