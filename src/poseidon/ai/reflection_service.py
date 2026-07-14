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
from .backends.base import ChatBackend
from .reflection import reflect_on_position

log = structlog.get_logger(__name__)

_CLOSING_SIDES = {OrderSide.SELL, OrderSide.SELL_TO_CLOSE, OrderSide.BUY_TO_CLOSE}
_WATERMARK_KEY = "reflection.fill_watermark"
_BENCHMARK = "SPY"


class ReflectionService:
    def __init__(self, *, db: Database, router: Any, config: ReflectionConfig,
                 model: str, get_backend: Callable[[], ChatBackend | None],
                 load_fills: Callable[[str | None], Awaitable[list[FillRecord]]],
                 is_flat: Callable[[str], bool],
                 audit_append: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
        self._db = db
        self._router = router
        self._config = config
        self._model = model
        self._get_backend = get_backend
        self._load_fills = load_fills
        self._is_flat = is_flat
        self._audit_append = audit_append
        self._tasks: set[asyncio.Task[None]] = set()

    async def on_account_synced(self, _event: Any = None) -> None:
        """Post-sync sweep: reflect on any position that just went flat.

        Driven by closing fills past a persisted watermark (a fully-closed
        symbol drops out of the portfolio, so scanning current holdings would
        miss it). The synced portfolio confirms flatness before reflecting.
        """
        if not self._config.enabled or self._get_backend() is None:
            return
        try:
            watermark: str = await self._db.kv_get(_WATERMARK_KEY, "")
            fills = await self._load_fills(None)
            newest = watermark
            candidates: list[str] = []
            for f in sorted(fills, key=lambda x: x.at.isoformat()):
                ts = f.at.isoformat()
                if ts <= watermark:
                    continue
                newest = max(newest, ts)
                if f.side in _CLOSING_SIDES and f.symbol not in candidates:
                    candidates.append(f.symbol)
            for symbol in candidates:
                if not self._is_flat(symbol):
                    continue  # still partially open — its final close is a later fill
                task = asyncio.create_task(self.reflect_episode(symbol))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            if newest != watermark:
                await self._db.kv_set(_WATERMARK_KEY, newest)
        except Exception as exc:  # never let reflection break the sync path
            log.warning("reflection sweep failed", error=str(exc))

    async def reflect_episode(self, symbol: str) -> None:
        try:
            backend = self._get_backend()
            if backend is None:
                return
            ep = latest_closed_episode(await self._load_fills(symbol))
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
            prose = await reflect_on_position(backend, pos, model=self._model)
            if not prose:
                return
            lesson = TradeLesson(
                id=uuid.uuid4().hex[:16], symbol=ep.symbol, strategy=ep.strategy,
                decision_id=ep.decision_id or None, entered_at=ep.entered_at,
                exited_at=ep.exited_at, realized_return=ep.realized_return, alpha=alpha,
                holding_days=ep.holding_days, lesson=prose, model=self._model,
                created_at=datetime.now(UTC))
            await self._db.add_trade_lesson(lesson)
            await self._audit_append("ai", "lesson_written",
                                     {"id": lesson.id, "symbol": ep.symbol})
        except Exception as exc:  # best-effort; a lost lesson is not a trading fault
            log.warning("reflection failed", symbol=symbol, error=str(exc))

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
