"""Reflection orchestration: detect closed positions, reflect, store, and serve
lessons back for cycle context.

Extracted from the kernel so it can be tested in isolation. Every dependency is
injected. Strictly advisory and off the execution hot path: the close sweep runs
on portfolio-sync events, reflection runs in background tasks, and any failure
logs and is swallowed — it never blocks a fill, an exit, or a review cycle, and
never touches the risk engine or the order path.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from ..analytics.performance import FillRecord
from ..analytics.reflection_data import benchmark_return, latest_closed_episode
from ..core.config import ReflectionConfig
from ..core.enums import OrderSide
from ..core.models import ClosedPosition, TradeLesson
from ..storage.db import Database
from .backends import sum_usage
from .backends.base import ChatBackend
from .reflection import reflect_on_position

log = structlog.get_logger(__name__)

_CLOSING_SIDES = {OrderSide.SELL, OrderSide.SELL_TO_CLOSE, OrderSide.BUY_TO_CLOSE}
_WATERMARK_KEY = "reflection.fill_watermark"
_BENCHMARK = "SPY"


class ReflectionService:
    def __init__(self, *, db: Database, router: Any, config: ReflectionConfig,
                 model: str, get_backend: Callable[[], ChatBackend | None],
                 load_fills: Callable[[str | None, str | None], Awaitable[list[FillRecord]]],
                 is_flat: Callable[[str], bool],
                 audit_append: Callable[[str, str, dict[str, Any]], Awaitable[Any]],
                 record_usage: Callable[[dict[str, int]], Awaitable[None]] | None = None,
                 over_budget: Callable[[], Awaitable[bool]] | None = None) -> None:
        self._db = db
        self._router = router
        self._config = config
        self._model = model
        self._get_backend = get_backend
        self._load_fills = load_fills
        self._is_flat = is_flat
        self._audit_append = audit_append
        self._record_usage = record_usage
        self._over_budget = over_budget
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopped = False

    async def stop(self, *, grace_seconds: float = 5.0) -> None:
        """Shutdown drain: refuse new sweeps, give in-flight reflections a
        short window to land their lesson write, then cancel stragglers.

        The kernel calls this before the backend, router, and DB close. A
        billed completion whose lesson write hits a closed DB is lost
        permanently — the fill watermark has already advanced past that
        episode's close, so no later run re-derives it.
        """
        self._stopped = True
        tasks = [t for t in self._tasks if not t.done()]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def on_account_synced(self, _topic: str, _payload: object) -> None:
        """Post-sync sweep: reflect on any position that just went flat.

        Driven by closing fills past a persisted watermark (a fully-closed
        symbol drops out of the portfolio, so scanning current holdings would
        miss it). The synced portfolio confirms flatness before reflecting.
        Skips outright once the monthly AI budget is exhausted, mirroring the
        chat/review-cycle gate — advisory spend never overruns the ceiling.
        """
        if not self._config.enabled or self._stopped or self._get_backend() is None:
            return
        try:
            if self._over_budget is not None and await self._over_budget():
                log.warning("monthly AI budget reached; skipping reflection sweep")
                return
            watermark: str = await self._db.kv_get(_WATERMARK_KEY, "")
            if not watermark:
                await self._seed_watermark()
                return
            # Bound the load to fills newer than the watermark in SQL, so a busy
            # ~30s sync never reloads the whole filled-order history.
            fills = sorted(await self._load_fills(None, watermark),
                           key=lambda x: x.at.isoformat())
            # Resolve flatness once per closed symbol against the synced
            # snapshot. A symbol that is not flat yet is deferred, not
            # consumed: the snapshot is fetched before the order poller may
            # persist the final close, so "not flat" can be a stale read of a
            # fully closed position.
            flat: dict[str, bool] = {}
            for f in fills:
                if f.side in _CLOSING_SIDES and f.symbol not in flat:
                    flat[f.symbol] = self._is_flat(f.symbol)
            # Advance the watermark only up to the first deferred close so the
            # next sweep re-sees it once the snapshot catches up (at-least-once;
            # reflect_episode dedups via lesson_exists, so re-seen fills of
            # already-reflected episodes are cheap DB checks, not LLM calls).
            newest = watermark
            for f in fills:
                if f.side in _CLOSING_SIDES and not flat[f.symbol]:
                    break
                newest = max(newest, f.at.isoformat())
            # A stop() that interleaved with this sweep wins: tasks spawned now
            # would race the closing backend/DB, and advancing the watermark
            # past their fills would make the missed lessons permanent.
            if self._stopped:
                return
            for symbol in (s for s, ok in flat.items() if ok):
                task = asyncio.create_task(self.reflect_episode(symbol))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            if newest != watermark:
                await self._db.kv_set(_WATERMARK_KEY, newest)
        except Exception as exc:  # never let reflection break the sync path
            log.warning("reflection sweep failed", error=str(exc))

    async def _seed_watermark(self) -> None:
        """First run: lessons start from now, not from the order history.

        A pre-existing database (fresh upgrade) can hold months of filled
        orders; sweeping them would burst one benchmark fetch plus one LLM
        completion per historical symbol and mint lessons for stale episodes.
        Seed the watermark to the newest existing fill (or now, on an empty
        book) so only closes from here on are reflected.
        """
        fills = await self._load_fills(None, None)
        seed = max((f.at.isoformat() for f in fills),
                   default=datetime.now(UTC).isoformat())
        await self._db.kv_set(_WATERMARK_KEY, seed)
        log.info("reflection watermark seeded", watermark=seed, skipped_fills=len(fills))

    async def reflect_episode(self, symbol: str) -> None:
        usage: list[dict[str, int]] = []
        try:
            backend = self._get_backend()
            if backend is None:
                return
            ep = latest_closed_episode(await self._load_fills(symbol, None))
            if ep is None:
                return
            if await self._db.lesson_exists(symbol, ep.entered_at, ep.exited_at):
                return
            thesis = await self._entry_thesis(ep.decision_id)
            bars = await self._router.bars(_BENCHMARK, timeframe="1d", limit=400)
            bench = benchmark_return(bars, ep.entered_at, ep.exited_at)
            alpha = None if bench is None else ep.realized_return - bench
            pos = ClosedPosition(
                symbol=ep.symbol, strategy=ep.strategy,
                decision_id=ep.decision_id or None, is_short=ep.is_short,
                quantity=ep.quantity, entry_price=ep.entry_price, exit_price=ep.exit_price,
                entered_at=ep.entered_at, exited_at=ep.exited_at,
                realized_return=ep.realized_return, alpha=alpha,
                holding_days=ep.holding_days, thesis=thesis)
            prose = await reflect_on_position(backend, pos, model=self._model, usage=usage)
            if not prose:
                return
            lesson = TradeLesson(
                id=uuid.uuid4().hex[:16], symbol=ep.symbol, strategy=ep.strategy,
                decision_id=ep.decision_id or None, entered_at=ep.entered_at,
                exited_at=ep.exited_at, realized_return=ep.realized_return, alpha=alpha,
                holding_days=ep.holding_days, lesson=prose,
                # Provenance: the model that actually wrote the prose (the
                # utility tier when tiering is on), not the configured primary.
                model=getattr(backend, "model", self._model),
                created_at=datetime.now(UTC))
            await self._db.add_trade_lesson(lesson)
            await self._audit_append("ai", "lesson_written",
                                     {"id": lesson.id, "symbol": ep.symbol})
        except Exception as exc:  # best-effort; a lost lesson is not a trading fault
            log.warning("reflection failed", symbol=symbol, error=str(exc))
        finally:
            # Meter spend even when the episode failed mid-pipeline, so the
            # monthly budget is never silently under-counted.
            if usage and self._record_usage is not None:
                try:
                    await self._record_usage(sum_usage(usage))
                except Exception as exc:
                    log.warning("reflection usage metering failed", error=str(exc))

    async def _entry_thesis(self, decision_id: str) -> str:
        if not decision_id:
            return ""
        row = await self._db.fetch_one(
            "SELECT payload FROM decisions WHERE id = ?", (decision_id,))
        if not row:
            return ""
        try:
            rat = json.loads(row[0]).get("rationale")
            return str(rat.get("thesis", "")) if isinstance(rat, dict) else ""
        except Exception:
            return ""

    async def relevant_lessons(self, symbols: list[str]) -> list[TradeLesson]:
        r = self._config
        if not (r.enabled and r.inject):
            return []
        try:
            return await self._db.recent_lessons(
                symbols, per_symbol=r.per_symbol, global_n=r.global_n,
                lookback_days=r.lookback_days, limit=r.max_injected, now=datetime.now(UTC))
        except Exception as exc:
            log.warning("lesson retrieval failed", error=str(exc))
            return []
