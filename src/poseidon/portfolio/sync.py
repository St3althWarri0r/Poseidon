"""Continuous portfolio synchronization.

Polls the primary broker for balances, buying power, positions, open
orders, fills, dividends, and tax lots; maintains day/week baselines for
the loss limits; persists equity marks for drawdown tracking across
restarts; and publishes ``portfolio.synced`` events.

Failures degrade gracefully: a failed sync marks the state stale (which
blocks trading via the risk engine's staleness rule) and retries with
backoff — the service itself never dies.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog

from ..brokers.base import Broker
from ..core.clock import MarketClock
from ..core.errors import BrokerError
from ..core.events import EventBus, Topics
from ..storage.db import Database
from .state import PortfolioState

log = structlog.get_logger(__name__)


class PortfolioSyncService:
    def __init__(self, broker: Broker, state: PortfolioState, bus: EventBus,
                 db: Database, clock: MarketClock, *, interval_seconds: float = 30.0) -> None:
        self._broker = broker
        self._state = state
        self._bus = bus
        self._db = db
        self._clock = clock
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._consecutive_failures = 0
        # Serializes sync passes against broker hot-swaps: a swap mid-pass
        # would otherwise produce a chimera snapshot (old broker's account,
        # new broker's positions) written under the new broker's name.
        self._sync_lock = asyncio.Lock()

    async def set_broker(self, broker: Broker) -> None:
        """Hot-swap the broker (Account view switch). Waits for any sync pass
        in flight so a snapshot is never assembled from two brokers; failure
        counters reset so a flap on the old broker does not carry a
        disconnected banner onto the new one."""
        async with self._sync_lock:
            self._broker = broker
            self._consecutive_failures = 0

    async def start(self) -> None:
        await self.restore_baselines()
        self._task = asyncio.create_task(self._loop(), name="portfolio-sync")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sync_once()
                self._consecutive_failures = 0
                delay = self._interval
            except BrokerError as exc:
                self._consecutive_failures += 1
                delay = min(self._interval * (2 ** min(self._consecutive_failures, 4)), 600)
                log.warning("portfolio sync failed", error=str(exc),
                            consecutive=self._consecutive_failures, retry_in=delay)
                if self._consecutive_failures == 3:
                    await self._bus.publish(Topics.BROKER_DISCONNECTED,
                                            {"broker": self._broker.name, "error": str(exc)})
            except Exception:
                self._consecutive_failures += 1
                delay = min(self._interval * 4, 600)
                log.exception("unexpected error in portfolio sync")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def sync_once(self) -> None:
        async with self._sync_lock:
            # Pin the broker for the whole pass: every fetch and the persisted
            # mark must come from ONE broker even if a swap lands mid-await.
            broker = self._broker
            account = await broker.account()
            positions = await broker.positions()
            open_orders = await broker.open_orders()
            fills = await broker.recent_fills(limit=50)
            lots = await broker.tax_lots()
            dividends = await broker.dividends(limit=50)

            was_disconnected = self._consecutive_failures >= 3
            now = datetime.now(UTC)

            self._state.account = account
            self._state.positions = positions
            self._state.open_orders = open_orders
            self._state.recent_fills = fills
            self._state.tax_lots = lots
            self._state.dividends = dividends
            self._state.synced_at = now
            self._state.record_equity(account.equity, now)

            await self._roll_baselines(now, broker.account_scope)
            await self._db.execute(
                "INSERT OR REPLACE INTO equity_marks (at, equity, cash, day_pnl, broker) "
                "VALUES (?, ?, ?, ?, ?)",
                (now.isoformat(), str(account.equity), str(account.cash),
                 str(account.day_pnl) if account.day_pnl is not None else None,
                 broker.account_scope),
            )
            if was_disconnected:
                await self._bus.publish(Topics.BROKER_RECONNECTED, {"broker": broker.name})
            await self._bus.publish(Topics.ACCOUNT_SYNCED, self._state.snapshot_dict())

    async def _roll_baselines(self, now: datetime, scope: str) -> None:
        """Reset day/week reference equity at session boundaries."""
        eastern_date = now.astimezone(self._clock.now_eastern().tzinfo).date()
        stored_day = await self._db.kv_get("baseline.day.date")
        equity = self._state.equity
        assert equity is not None
        await self._db.kv_set("baseline.broker", scope)
        if stored_day != eastern_date.isoformat():
            self._state.day_start_equity = equity
            await self._db.kv_set("baseline.day.date", eastern_date.isoformat())
            await self._db.kv_set("baseline.day.equity", str(equity))
            # New ISO week?
            week_key = f"{eastern_date.isocalendar().year}-W{eastern_date.isocalendar().week}"
            stored_week = await self._db.kv_get("baseline.week.key")
            if stored_week != week_key:
                self._state.week_start_equity = equity
                await self._db.kv_set("baseline.week.key", week_key)
                await self._db.kv_set("baseline.week.equity", str(equity))

    async def restore_baselines(self) -> None:
        """Reload baselines and peak equity from the DB — at startup and
        after a broker switch.

        Everything is scoped to the CURRENT broker+environment: baselines and
        peaks that belong to a previously active account (e.g. the paper
        account before a real brokerage was connected, or alpaca-paper before
        alpaca-live) must never carry over — a paper peak would otherwise
        read as a massive drawdown on a smaller real account and halt
        trading. Rows from before marks were broker-scoped (broker='') are
        deliberately excluded rather than guessed at: v2.1 recorded no broker
        identity, so attributing them to anyone would be fabrication."""
        from decimal import Decimal

        async with self._sync_lock:
            scope = self._broker.account_scope
            baseline_broker = await self._db.kv_get("baseline.broker")
            if baseline_broker in (None, "", scope):
                day_equity = await self._db.kv_get("baseline.day.equity")
                week_equity = await self._db.kv_get("baseline.week.equity")
                # Trust a baseline only when its boundary key is also intact
                # (a cleared date with a leftover equity means a reset was in
                # progress — re-baseline rather than restore half a state).
                if day_equity and await self._db.kv_get("baseline.day.date"):
                    self._state.day_start_equity = Decimal(day_equity)
                if week_equity and await self._db.kv_get("baseline.week.key"):
                    self._state.week_start_equity = Decimal(week_equity)
            else:
                # Account changed since these baselines were written: force a
                # re-baseline on the next sync instead of inheriting another
                # account's numbers.
                await self._db.kv_set("baseline.day.date", "")
                await self._db.kv_set("baseline.week.key", "")
            row = await self._db.fetch_one(
                "SELECT MAX(CAST(equity AS REAL)) FROM equity_marks WHERE broker = ?",
                (scope,),
            )
            self._state.peak_equity = (
                Decimal(str(row[0])) if row and row[0] is not None else None
            )
