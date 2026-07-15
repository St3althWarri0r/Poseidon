"""Analyst-firm orchestration: scheduled sweep → per-symbol packet → serve back.

Sibling of ReflectionService. Strictly advisory and off the execution hot path:
the sweep runs on a scheduler tick, each symbol's firm runs best-effort in the
background, and any failure logs and is swallowed. Packets are injected into the
review-cycle prompt only; they never reach the risk engine or the order path."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from ..core.config import AnalysisConfig
from ..core.models import AnalysisPacket
from ..storage.db import Database
from .analysis.analysts import run_analysts
from .analysis.debate import run_debate
from .analysis.packet import assemble
from .analysis.risk_lens import run_risk_lens
from .analysis.snapshot import build_snapshot
from .backends.base import ChatBackend

log = structlog.get_logger(__name__)


class AnalysisService:
    def __init__(self, *, db: Database, router: Any, config: AnalysisConfig, model: str,
                 get_backend: Callable[[], ChatBackend | None],
                 watchlist: Callable[[], list[str]],
                 audit_append: Callable[[str, str, dict[str, Any]], Awaitable[Any]],
                 scan: Callable[[str], str] | None = None) -> None:
        self._db = db
        self._router = router
        self._config = config
        self._model = model
        self._get_backend = get_backend
        self._watchlist = watchlist
        self._audit_append = audit_append
        self._scan = scan
        self._tasks: set[asyncio.Task[None]] = set()

    async def run_sweep(self, _topic: str | None = None, _payload: object = None) -> None:
        if not self._config.enabled or self._get_backend() is None:
            return
        try:
            now = datetime.now(UTC)
            symbols = self._watchlist()[: self._config.max_symbols_per_sweep]
            for symbol in symbols:
                if await self._db.packet_fresh(
                        symbol, refresh_hours=self._config.refresh_hours, now=now):
                    continue
                task = asyncio.create_task(self.analyze_symbol(symbol))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception as exc:  # never break the scheduler tick
            log.warning("analysis sweep failed", error=str(exc))

    async def analyze_symbol(self, symbol: str) -> None:
        try:
            backend = self._get_backend()
            if backend is None:
                return
            snap = await build_snapshot(self._router, symbol)
            if snap is None:
                return
            reports = await run_analysts(backend, snap, context="", scan=self._scan)
            verdict = await run_debate(backend, reports, rounds=self._config.debate_rounds)
            lens = await run_risk_lens(backend, verdict, reports,
                                       rounds=self._config.risk_rounds)
            packet = assemble(packet_id=uuid.uuid4().hex[:16], symbol=symbol, snapshot=snap,
                              reports=reports, verdict=verdict, risk_lens=lens,
                              model=self._model)
            await self._db.add_analysis_packet(packet)
            await self._audit_append("ai", "analysis_packet_written",
                                     {"id": packet.id, "symbol": symbol})
        except Exception as exc:  # best-effort; a lost packet is not a trading fault
            log.warning("analysis failed", symbol=symbol, error=str(exc))

    async def relevant_packets(self, symbols: list[str]) -> list[AnalysisPacket]:
        c = self._config
        if not (c.enabled and c.inject):
            return []
        try:
            return await self._db.recent_packets(
                symbols, refresh_hours=c.refresh_hours, limit=c.max_injected,
                now=datetime.now(UTC))
        except Exception as exc:
            log.warning("packet retrieval failed", error=str(exc))
            return []
