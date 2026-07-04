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

    def set_broker(self, broker: Broker) -> None:
        """Hot-swap the broker (Account view switch). The next sync pulls the
        new account; failure counters reset so a flap on the old broker does
        not carry a disconnected banner onto the new one."""
        self._broker = broker
        self._consecutive_failures = 0

    async def start(self) -> None:
        await self._restore_baselines()
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
        account = await self._broker.account()
        positions = await self._broker.positions()
        open_orders = await self._broker.open_orders()
        fills = await self._broker.recent_fills(limit=50)
        lots = await self._broker.tax_lots()
        dividends = await self._broker.dividends(limit=50)

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

        await self._roll_baselines(now)
        await self._db.execute(
            "INSERT OR REPLACE INTO equity_marks (at, equity, cash, day_pnl, broker) "
            "VALUES (?, ?, ?, ?, ?)",
            (now.isoformat(), str(account.equity), str(account.cash),
             str(account.day_pnl) if account.day_pnl is not None else None,
             self._broker.name),
        )
        if was_disconnected:
            await self._bus.publish(Topics.BROKER_RECONNECTED, {"broker": self._broker.name})
        await self._bus.publish(Topics.ACCOUNT_SYNCED, self._state.snapshot_dict())

    async def _roll_baselines(self, now: datetime) -> None:
        """Reset day/week reference equity at session boundaries."""
        eastern_date = now.astimezone(self._clock.now_eastern().tzinfo).date()
        stored_day = await self._db.kv_get("baseline.day.date")
        equity = self._state.equity
        assert equity is not None
        await self._db.kv_set("baseline.broker", self._broker.name)
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

    async def _restore_baselines(self) -> None:
        """Crash recovery: reload baselines and peak equity from the DB.

        Everything is scoped to the CURRENT broker: baselines and peaks that
        belong to a previously active broker (e.g. the paper account before a
        real brokerage was connected) must never carry over — a paper peak
        would otherwise read as a massive drawdown on a smaller real account
        and halt trading."""
        from decimal import Decimal

        # One-time upgrade backfill: rows written before marks were
        # broker-scoped all belong to the broker active at upgrade time.
        if not await self._db.kv_get("equity_marks.broker_backfilled"):
            await self._db.execute(
                "UPDATE equity_marks SET broker = ? WHERE broker = ''", (self._broker.name,)
            )
            await self._db.kv_set("equity_marks.broker_backfilled", True)

        baseline_broker = await self._db.kv_get("baseline.broker")
        if baseline_broker in (None, "", self._broker.name):
            day_equity = await self._db.kv_get("baseline.day.equity")
            week_equity = await self._db.kv_get("baseline.week.equity")
            if day_equity:
                self._state.day_start_equity = Decimal(day_equity)
            if week_equity:
                self._state.week_start_equity = Decimal(week_equity)
        else:
            # Broker changed since the last run: force a re-baseline on the
            # next sync instead of inheriting another account's numbers.
            await self._db.kv_set("baseline.day.date", "")
            await self._db.kv_set("baseline.week.key", "")
        row = await self._db.fetch_one(
            "SELECT MAX(CAST(equity AS REAL)) FROM equity_marks WHERE broker = ?",
            (self._broker.name,),
        )
        if row and row[0] is not None:
            self._state.peak_equity = Decimal(str(row[0]))
