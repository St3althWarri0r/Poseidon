"""Analyst-firm orchestration: scheduled sweep → per-symbol packet → serve back.

Sibling of ReflectionService. Strictly advisory and off the execution hot path:
the sweep runs on a scheduler tick, each symbol's firm runs best-effort in the
background, and any failure logs and is swallowed. Packets are injected into the
review-cycle prompt only; they never reach the risk engine or the order path."""
from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from ..core.config import AnalysisConfig, SnapshotConfig
from ..core.models import AnalysisPacket
from ..storage.db import Database
from .analysis.analysts import run_analysts
from .analysis.debate import run_debate
from .analysis.packet import assemble
from .analysis.risk_lens import run_risk_lens
from .analysis.snapshot import build_snapshot
from .backends import sum_usage
from .backends.base import ChatBackend

log = structlog.get_logger(__name__)


class AnalysisService:
    def __init__(self, *, db: Database, router: Any, config: AnalysisConfig, model: str,
                 get_backend: Callable[[], ChatBackend | None],
                 watchlist: Callable[[], list[str]],
                 audit_append: Callable[[str, str, dict[str, Any]], Awaitable[Any]],
                 scan: Callable[[str], str] | None = None,
                 record_usage: Callable[[dict[str, int]], Awaitable[None]] | None = None,
                 over_budget: Callable[[], Awaitable[bool]] | None = None,
                 snapshot_config: SnapshotConfig | None = None) -> None:
        self._db = db
        self._router = router
        self._config = config
        self._model = model
        self._snapshot_config = snapshot_config or SnapshotConfig()
        self._get_backend = get_backend
        self._watchlist = watchlist
        self._audit_append = audit_append
        self._scan = scan
        self._record_usage = record_usage
        self._over_budget = over_budget
        self._tasks: set[asyncio.Task[None]] = set()
        # Symbols with an analysis pipeline currently in flight. packet_fresh
        # only sees *written* packets, so without this a sweep tick that fires
        # while a prior tick's minutes-long pipeline is still running would
        # spawn a duplicate pipeline for the same symbol.
        self._inflight: set[str] = set()
        self._stopped = False

    async def stop(self, *, grace_seconds: float = 5.0) -> None:
        """Shutdown drain: refuse new sweeps, give in-flight pipelines a short
        window to land their packet write, then cancel stragglers.

        The kernel calls this before the backend, router, and DB close. Unlike
        lessons, a cancelled pipeline self-heals (packet_fresh only sees
        written packets, so the next sweep recomputes), but the grace window
        keeps a near-complete run's billed completions from being wasted.
        """
        self._stopped = True
        tasks = [t for t in self._tasks if not t.done()]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def run_sweep(self, _topic: str | None = None, _payload: object = None) -> None:
        if not self._config.enabled or self._stopped or self._get_backend() is None:
            return
        try:
            # The firm is call-heavy (up to ~13 completions per symbol), so the
            # monthly budget gates the whole sweep, mirroring chat/review cycles.
            if self._over_budget is not None and await self._over_budget():
                log.warning("monthly AI budget reached; skipping analysis sweep")
                return
            now = datetime.now(UTC)
            symbols = self._watchlist()[: self._config.max_symbols_per_sweep]
            # Recompute at half the refresh window, not the full window. The
            # full refresh_hours is also the inject-staleness bound (see
            # relevant_packets/recent_packets below) and is ~ the default
            # sweep cadence, so gating recompute on the full window can make a
            # packet "too fresh to recompute" yet "too stale to inject" by the
            # next sweep. Recomputing at half-life keeps injected packets
            # within the inject window regardless of cadence drift.
            recompute_hours = max(1, self._config.refresh_hours // 2)
            for symbol in symbols:
                # Re-check between awaits: a stop() that interleaved with this
                # sweep wins — new pipelines would race the closing backend/DB.
                if self._stopped:
                    return
                if symbol in self._inflight or await self._db.packet_fresh(
                        symbol, refresh_hours=recompute_hours, now=now):
                    continue
                self._inflight.add(symbol)
                task = asyncio.create_task(self.analyze_symbol(symbol))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                task.add_done_callback(functools.partial(self._release_inflight, symbol))
        except Exception as exc:  # never break the scheduler tick
            log.warning("analysis sweep failed", error=str(exc))

    def _release_inflight(self, symbol: str, _task: asyncio.Task[None]) -> None:
        self._inflight.discard(symbol)

    async def analyze_symbol(self, symbol: str) -> None:
        usage: list[dict[str, int]] = []
        try:
            backend = self._get_backend()
            if backend is None:
                return
            snap = await build_snapshot(self._router, symbol, config=self._snapshot_config)
            if snap is None:
                return
            reports = await run_analysts(backend, snap, context="", scan=self._scan,
                                         usage=usage)
            verdict = await run_debate(backend, reports, rounds=self._config.debate_rounds,
                                       usage=usage)
            # A fully degraded run (backend outage: every analyst empty AND the
            # debate produced nothing) carries no signal. Persisting it would
            # inject an empty "avoid" packet as the firm view and, because
            # packet_fresh keys on as_of, suppress recomputation after the
            # backend recovers. Partially degraded runs are kept — the empty
            # reports stay flagged via their data_gaps.
            if all(not r.summary for r in reports) and not (
                    verdict.synthesis or verdict.bull_case or verdict.bear_case):
                log.warning("analysis fully degraded; packet not persisted", symbol=symbol)
                return
            lens = await run_risk_lens(backend, verdict, reports,
                                       rounds=self._config.risk_rounds, usage=usage)
            packet = assemble(packet_id=uuid.uuid4().hex[:16], symbol=symbol, snapshot=snap,
                              reports=reports, verdict=verdict, risk_lens=lens,
                              # Provenance: the model that actually ran the firm
                              # (the utility tier when tiering is on).
                              model=getattr(backend, "model", self._model))
            await self._db.add_analysis_packet(packet)
            await self._audit_append("ai", "analysis_packet_written",
                                     {"id": packet.id, "symbol": symbol})
        except Exception as exc:  # best-effort; a lost packet is not a trading fault
            log.warning("analysis failed", symbol=symbol, error=str(exc))
        finally:
            # Meter spend even when the pipeline failed partway, so the monthly
            # budget is never silently under-counted.
            if usage and self._record_usage is not None:
                try:
                    await self._record_usage(sum_usage(usage))
                except Exception as exc:
                    log.warning("analysis usage metering failed", error=str(exc))

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
