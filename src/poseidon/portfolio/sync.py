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
from datetime import UTC, date, datetime, time
from decimal import Decimal

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
        self._disconnect_notified = False
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
            self._disconnect_notified = False

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
                if self._consecutive_failures >= 3 and not self._disconnect_notified:
                    self._disconnect_notified = True
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

            was_disconnected = self._disconnect_notified
            now = datetime.now(UTC)

            self._state.account = account
            self._state.positions = positions
            self._state.open_orders = open_orders
            self._state.recent_fills = fills
            self._state.tax_lots = lots
            self._state.dividends = dividends
            self._state.synced_at = now
            # A DIFFERENT real account at the same broker scope (account_scope
            # is name:paper|live and cannot tell two live accounts apart) must
            # not inherit the prior account's loss/drawdown anchors. Detect the
            # account_id change and drop the restored baselines so the
            # re-baseline below re-anchors to THIS account on the same pass.
            await self._reanchor_on_account_change(account.account_id)
            # Re-anchor for deposits/withdrawals BEFORE recording equity: the
            # peak must shift before a deposit-inflated mark can ratchet it.
            await self._apply_external_flows(broker, now)
            self._state.record_equity(account.equity, now)
            # Persist the flow-adjusted peak so a restart cannot resurrect a
            # pre-withdrawal MAX(equity_marks) as a phantom drawdown.
            if self._state.peak_equity is not None:
                await self._db.kv_set("baseline.peak.equity", str(self._state.peak_equity))

            await self._roll_baselines(now, broker.account_scope)
            # Persist the flow-adjusted / intraday troughs (mirrors the peak at
            # 125-126) so a same-day restart restores the true session low instead
            # of rebuilding a pre-deposit low from raw marks — a phantom drawdown
            # that would latch the loss/drawdown halts. Written AFTER
            # _roll_baselines because that resets the troughs at a day/week boundary.
            if self._state.day_min_equity is not None:
                await self._db.kv_set("baseline.day.min", str(self._state.day_min_equity))
            if self._state.week_min_equity is not None:
                await self._db.kv_set("baseline.week.min", str(self._state.week_min_equity))
            await self._db.execute(
                "INSERT OR REPLACE INTO equity_marks (at, equity, cash, day_pnl, broker) "
                "VALUES (?, ?, ?, ?, ?)",
                (now.isoformat(), str(account.equity), str(account.cash),
                 str(account.day_pnl) if account.day_pnl is not None else None,
                 broker.account_scope),
            )
            # A successful pass proves the broker healthy no matter who
            # triggered it (loop or the dashboard's Sync now) — reset the
            # failure streak so backoff ends and reconnect fires only once.
            self._consecutive_failures = 0
            self._disconnect_notified = False
            if was_disconnected:
                await self._bus.publish(Topics.BROKER_RECONNECTED, {"broker": broker.name})
            await self._bus.publish(Topics.ACCOUNT_SYNCED, self._state.snapshot_dict())

    async def _reanchor_on_account_change(self, account_id: str) -> None:
        """Clear the restored loss/drawdown baselines when the connected
        account_id differs from the one they belong to.

        ``account_scope`` (name:paper|live) is identical for two different real
        accounts at the same brokerage, so ``restore_baselines`` happily
        restores account A's day/week/peak anchors onto account B. Left as-is
        the loss and drawdown halts measure B against A's numbers — a bigger B
        never halts; a smaller B latches a phantom drawdown. Here we detect the
        change and reset the in-memory anchors plus the persisted boundary keys;
        ``record_equity`` (peak/troughs from None) and ``_roll_baselines`` (day/
        week from the cleared keys) then re-anchor to B's equity on this pass.

        Note (minimal-fix limitation): ``equity_marks`` remain keyed by
        ``account_scope``, so two accounts at the same scope still share an
        equity table; this fixes the safety-critical halt anchoring, not the
        equity-curve mixing (that needs an account_id-scoped mark key)."""
        stored_id = await self._db.kv_get("baseline.account_id")
        if stored_id and account_id and stored_id != account_id:
            self._state.day_start_equity = None
            self._state.week_start_equity = None
            self._state.peak_equity = None
            self._state.day_min_equity = None
            self._state.week_min_equity = None
            await self._db.kv_set("baseline.day.date", "")
            await self._db.kv_set("baseline.week.key", "")
            await self._db.kv_set("baseline.peak.equity", "")
            await self._db.kv_set("baseline.day.min", "")
            await self._db.kv_set("baseline.week.min", "")
            await self._db.kv_set("baseline.flows.cursor", "")
            log.warning("account changed within the same broker scope; loss/drawdown "
                        "baselines re-anchored to the new account",
                        was=stored_id, now=account_id)
        if account_id:
            await self._db.kv_set("baseline.account_id", account_id)

    async def _apply_external_flows(self, broker: Broker, now: datetime) -> None:
        """Shift the loss/drawdown anchors by net deposits/withdrawals.

        External cash flows are not trading P&L: unadjusted, a withdrawal
        reads as a permanent drawdown and a deposit masks a genuine
        limit-breaching loss. Every non-None anchor moves by the net flow so
        the halt percentages keep measuring trading only. Brokers without
        transfer visibility (the default ``Broker.transfers``) report none
        and nothing changes."""
        cursor = await self._db.kv_get("baseline.flows.cursor")
        if not cursor:
            # First pass for this account: anchor the cursor NOW — historical
            # transfers predate the baselines and must never be replayed.
            await self._db.kv_set("baseline.flows.cursor", now.isoformat())
            return
        xfers = await broker.transfers(since=datetime.fromisoformat(cursor))
        if not xfers:
            return
        net = sum((t.amount for t in xfers), Decimal(0))
        if net != 0:
            if self._state.day_start_equity is not None:
                self._state.day_start_equity += net
                await self._db.kv_set("baseline.day.equity", str(self._state.day_start_equity))
            if self._state.week_start_equity is not None:
                self._state.week_start_equity += net
                await self._db.kv_set("baseline.week.equity", str(self._state.week_start_equity))
            if self._state.day_min_equity is not None:
                self._state.day_min_equity += net
            if self._state.week_min_equity is not None:
                self._state.week_min_equity += net
            if self._state.peak_equity is not None:
                self._state.peak_equity += net
            log.warning("external cash flow re-anchored loss baselines",
                        broker=broker.name, net=str(net), transfers=len(xfers))
            await self._bus.publish(Topics.NOTIFY, {
                "level": "warning",
                "title": "External cash flow detected",
                "body": (f"Net {'deposit' if net > 0 else 'withdrawal'} of {abs(net)} — "
                         "the loss/drawdown baselines were re-anchored so the "
                         "halts keep measuring trading P&L only."),
            })
        await self._db.kv_set("baseline.flows.cursor", max(t.at for t in xfers).isoformat())

    async def _roll_baselines(self, now: datetime, scope: str) -> None:
        """Reset day/week reference equity at session boundaries."""
        eastern_date = now.astimezone(self._clock.now_eastern().tzinfo).date()
        stored_day = await self._db.kv_get("baseline.day.date")
        equity = self._state.equity
        assert equity is not None
        await self._db.kv_set("baseline.broker", scope)
        if stored_day != eastern_date.isoformat():
            self._state.day_start_equity = equity
            self._state.day_min_equity = equity  # clear the intraday loss/drawdown latch
            await self._db.kv_set("baseline.day.date", eastern_date.isoformat())
            await self._db.kv_set("baseline.day.equity", str(equity))
            # Persist the reset trough in the SAME boundary block as its date/equity
            # key, so a crash cannot leave a new-day date paired with a stale trough
            # (which restore would read as a phantom drawdown, F020). The same-day
            # ratchet is persisted separately in sync_once.
            await self._db.kv_set("baseline.day.min", str(equity))
            # New ISO week?
            week_key = f"{eastern_date.isocalendar().year}-W{eastern_date.isocalendar().week}"
            stored_week = await self._db.kv_get("baseline.week.key")
            if stored_week != week_key:
                self._state.week_start_equity = equity
                self._state.week_min_equity = equity  # clear the weekly loss latch
                await self._db.kv_set("baseline.week.key", week_key)
                await self._db.kv_set("baseline.week.equity", str(equity))
                await self._db.kv_set("baseline.week.min", str(equity))

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
        async with self._sync_lock:
            scope = self._broker.account_scope
            baseline_broker = await self._db.kv_get("baseline.broker")
            peak: Decimal | None = None
            if baseline_broker in (None, "", scope):
                day_equity = await self._db.kv_get("baseline.day.equity")
                week_equity = await self._db.kv_get("baseline.week.equity")
                day_date = await self._db.kv_get("baseline.day.date")
                week_key = await self._db.kv_get("baseline.week.key")
                # Trust a baseline only when its boundary key is also intact
                # (a cleared date with a leftover equity means a reset was in
                # progress — re-baseline rather than restore half a state).
                if day_equity and day_date:
                    self._state.day_start_equity = Decimal(day_equity)
                    # Restore the session trough so the intraday loss/drawdown
                    # halt latch survives a restart — record_equity would
                    # otherwise seed it with the (possibly recovered) current
                    # equity and silently clear the halt. Prefer the persisted
                    # flow-adjusted trough (baseline.day.min) over the raw-marks
                    # rebuild, which resurrects a pre-deposit low as a phantom
                    # drawdown (F020); fall back to _min_mark_since for installs
                    # with no persisted trough yet. If the stored date is stale,
                    # _roll_baselines resets this on the first sync.
                    stored_day_min = await self._db.kv_get("baseline.day.min")
                    self._state.day_min_equity = (
                        Decimal(stored_day_min) if stored_day_min
                        else await self._min_mark_since(scope, date.fromisoformat(day_date)))
                if week_equity and week_key:
                    self._state.week_start_equity = Decimal(week_equity)
                    year_s, _, week_s = week_key.partition("-W")
                    monday = date.fromisocalendar(int(year_s), int(week_s), 1)
                    stored_week_min = await self._db.kv_get("baseline.week.min")
                    self._state.week_min_equity = (
                        Decimal(stored_week_min) if stored_week_min
                        else await self._min_mark_since(scope, monday))
                # Prefer the flow-adjusted peak persisted by sync_once — the
                # raw MAX(equity_marks) fallback would resurrect a
                # pre-withdrawal peak as a phantom drawdown.
                stored_peak = await self._db.kv_get("baseline.peak.equity")
                if stored_peak:
                    peak = Decimal(stored_peak)
            else:
                # Account changed since these baselines were written: force a
                # re-baseline on the next sync instead of inheriting another
                # account's numbers.
                await self._db.kv_set("baseline.day.date", "")
                await self._db.kv_set("baseline.week.key", "")
                await self._db.kv_set("baseline.peak.equity", "")
                await self._db.kv_set("baseline.day.min", "")
                await self._db.kv_set("baseline.week.min", "")
                await self._db.kv_set("baseline.flows.cursor", "")
            if peak is None:
                row = await self._db.fetch_one(
                    "SELECT MAX(CAST(equity AS REAL)) FROM equity_marks WHERE broker = ?",
                    (scope,),
                )
                peak = Decimal(str(row[0])) if row and row[0] is not None else None
            self._state.peak_equity = peak

    async def _min_mark_since(self, scope: str, day: date) -> Decimal | None:
        """Lowest persisted equity mark since Eastern midnight of ``day`` —
        rebuilds the day/week trough latches across restarts. Marks and the
        cutoff are both UTC isoformat strings, so the string comparison is
        time-consistent."""
        tz = self._clock.now_eastern().tzinfo
        since = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC).isoformat()
        row = await self._db.fetch_one(
            "SELECT MIN(CAST(equity AS REAL)) FROM equity_marks WHERE broker = ? AND at >= ?",
            (scope, since),
        )
        return Decimal(str(row[0])) if row and row[0] is not None else None
