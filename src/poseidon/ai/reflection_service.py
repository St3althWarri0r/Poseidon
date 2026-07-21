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
import math
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from ..analytics.behavior import compute_bias_profile
from ..analytics.performance import FillRecord, build_round_trips
from ..analytics.reflection_data import benchmark_return, forward_return, latest_closed_episode
from ..core.config import OutcomeResolutionConfig, ReflectionConfig
from ..core.enums import OrderSide
from ..core.models import Bar, ClosedPosition, DecisionOutcome, TradeLesson
from ..core.symbols import is_crypto_symbol
from ..storage.db import Database
from .backends import sum_usage
from .backends.base import ChatBackend
from .reflection import reflect_on_outcome, reflect_on_position

log = structlog.get_logger(__name__)

_CLOSING_SIDES = {OrderSide.SELL, OrderSide.SELL_TO_CLOSE, OrderSide.BUY_TO_CLOSE}
_WATERMARK_KEY = "reflection.fill_watermark"
_OUTCOME_WATERMARK_KEY = "reflection.outcome_watermark"
# Outcome-sweep constants: proposal sides that OPEN risk (sell_to_open inverts
# the forward-return sign), order statuses that mean the decision executed, and
# the per-decision grading fan-out bound.
_OPEN_SIDES = {"buy", "buy_to_open", "sell_to_open"}
_EXECUTED_STATUSES = {"filled", "partially_filled"}
_MAX_SYMBOLS_PER_DECISION = 3


def _risk_case_from_payload(payload: dict[str, Any]) -> tuple[str, float | None, str]:
    """(thesis, confidence, invalidation) from a stored decision payload dict.

    Best-effort: legacy rows without the risk-case fields (or with junk in
    them) yield ("", None, "") shapes rather than failing the caller —
    reflection proceeds, just without those lines.
    """
    try:
        rat = payload.get("rationale")
        if not isinstance(rat, dict):
            return "", None, ""
        raw_conf = rat.get("confidence")
        # bool subclasses int, and NaN slips through a min/max clamp (every
        # comparison is False) only to blow up the model's ge/le bounds — both
        # are row junk that must degrade, not kill the episode.
        confidence = None
        if (isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool)
                and math.isfinite(raw_conf)):
            confidence = min(max(float(raw_conf), 0.0), 1.0)
        raw_inval = rat.get("invalidation")
        return (str(rat.get("thesis", "")), confidence,
                raw_inval.strip() if isinstance(raw_inval, str) else "")
    except Exception:
        return "", None, ""


def _forward_window(bars: list[Bar], start: datetime, horizon: int,
                    ) -> tuple[datetime, datetime] | None:
    """(entry bar end, exit bar end) under forward_return's exact indexing —
    the equity-marks probe timestamps for the hold grade. Kept tiny and local:
    reflection_data deliberately exposes only the return itself."""
    ordered = sorted(bars, key=lambda b: b.end)
    entry = next((i for i, b in enumerate(ordered) if b.end >= start), None)
    if entry is None or entry + horizon >= len(ordered):
        return None
    return ordered[entry].end, ordered[entry + horizon].end


class ReflectionService:
    def __init__(self, *, db: Database, router: Any, config: ReflectionConfig,
                 model: str, get_backend: Callable[[], ChatBackend | None],
                 load_fills: Callable[[str | None, str | None], Awaitable[list[FillRecord]]],
                 is_flat: Callable[[str], bool],
                 audit_append: Callable[[str, str, dict[str, Any]], Awaitable[Any]],
                 record_usage: Callable[[dict[str, int]], Awaitable[None]] | None = None,
                 over_budget: Callable[[], Awaitable[bool]] | None = None,
                 benchmark_symbol: str = "SPY",
                 crypto_benchmark_symbol: str = "BTC/USD",
                 account_scope: Callable[[], str] | None = None) -> None:
        """``benchmark_symbol``/``crypto_benchmark_symbol`` are the two-class
        asset-to-benchmark map (risk.benchmark_symbol and its crypto
        companion); ``account_scope`` is a call-time callable (not a
        construction-time string) because broker hot-toggle exists and fill
        loading is already call-time scoped."""
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
        self._benchmark_symbol = benchmark_symbol
        self._crypto_benchmark_symbol = crypto_benchmark_symbol
        self._account_scope = account_scope
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopped = False

    def _benchmark_for(self, symbol: str) -> str:
        return (self._crypto_benchmark_symbol if is_crypto_symbol(symbol)
                else self._benchmark_symbol)

    def _scope(self) -> str:
        return self._account_scope() if self._account_scope is not None else ""

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
            thesis, entry_confidence, invalidation = await self._entry_risk_case(ep.decision_id)
            benchmark = self._benchmark_for(ep.symbol)
            bars = await self._router.bars(benchmark, timeframe="1d", limit=400)
            bench = benchmark_return(bars, ep.entered_at, ep.exited_at)
            alpha = None if bench is None else ep.realized_return - bench
            pos = ClosedPosition(
                symbol=ep.symbol, strategy=ep.strategy,
                decision_id=ep.decision_id or None, is_short=ep.is_short,
                quantity=ep.quantity, entry_price=ep.entry_price, exit_price=ep.exit_price,
                entered_at=ep.entered_at, exited_at=ep.exited_at,
                realized_return=ep.realized_return, alpha=alpha,
                holding_days=ep.holding_days, thesis=thesis,
                entry_confidence=entry_confidence, invalidation=invalidation,
                benchmark=benchmark)
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

    async def _entry_risk_case(self, decision_id: str) -> tuple[str, float | None, str]:
        """(thesis, confidence, invalidation) from the stored entry decision.

        Fetch + delegate to :func:`_risk_case_from_payload` (the outcome sweep
        reuses the parse on payloads it already holds). Best-effort like the
        old thesis lookup: junk degrades to ("", None, "") shapes.
        """
        if not decision_id:
            return "", None, ""
        row = await self._db.fetch_one(
            "SELECT payload FROM decisions WHERE id = ?", (decision_id,))
        if not row:
            return "", None, ""
        try:
            return _risk_case_from_payload(json.loads(row[0]))
        except Exception:
            return "", None, ""

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

    # -- decision outcome resolution (scheduled; advisory) ---------------------

    async def resolve_outcomes(self) -> None:
        """Scheduled sweep: grade decisions that never became trades — HOLDs,
        risk-vetoed and human-rejected proposals, never-filled orders — at the
        configured forward horizon, and mint counterfactual/hold lessons for
        the material ones.

        Two phases. PHASE 1 computes every resolution with ZERO writes; a row
        whose forward bars do not exist yet stays PENDING (skip-and-retry)
        until ``max_age_days`` turns it terminally ``unresolvable``. PHASE 2
        writes the ranked/capped lessons BEFORE any resolution marker, so a
        crash between them re-grades next sweep and REPLACEs the same
        deterministic id — no duplicates, no losses. The AI budget gates the
        LLM lesson phase only: markers are deterministic bookkeeping and still
        commit (deliberate divergence from on_account_synced's whole-sweep
        skip). Never raises; never touches the risk engine or the order path.
        """
        cfg = self._config
        if not (cfg.enabled and cfg.outcomes.enabled) or self._stopped:
            return
        usage: list[dict[str, int]] = []
        try:
            oc = cfg.outcomes
            now = datetime.now(UTC)
            watermark: str = await self._db.kv_get(_OUTCOME_WATERMARK_KEY, "")
            if not watermark:
                # Static floor, seeded once: pre-feature decisions are NEVER
                # graded (the fill-watermark convention); resolved_at markers
                # do the advancing.
                await self._db.kv_set(_OUTCOME_WATERMARK_KEY, now.isoformat())
                log.info("outcome watermark seeded", watermark=now.isoformat())
                return
            # Necessary calendar pre-filter (cheap, not sufficient): N trading
            # bars span >= N calendar days, so a younger decision cannot have
            # its forward bars yet.
            before = (now - timedelta(days=oc.horizon_trading_days)).isoformat()
            rows = await self._db.unresolved_decisions(
                after=watermark, before=before, limit=oc.max_decisions_per_sweep)
            # One bars fetch per symbol per sweep, shared across decisions AND
            # benchmarks (25 SPY-benchmarked decisions = 1 SPY fetch); None
            # caches a failure so a down provider is not hammered mid-sweep.
            bars_cache: dict[str, list[Bar] | None] = {}

            async def cached_bars(symbol: str) -> list[Bar] | None:
                if symbol not in bars_cache:
                    try:
                        bars_cache[symbol] = await self._router.bars(
                            symbol, timeframe="1d", limit=400)
                    except Exception as exc:
                        log.warning("outcome bars fetch failed", symbol=symbol,
                                    error=str(exc))
                        bars_cache[symbol] = None
                return bars_cache[symbol]

            # ---- PHASE 1: compute (zero writes) ----------------------------
            resolutions: list[tuple[str, dict[str, Any]]] = []
            candidates: list[tuple[DecisionOutcome, float]] = []
            pending = 0
            for did, action, payload_json, created_iso in rows:
                if self._stopped:  # honor kernel shutdown ordering between rows
                    return
                try:
                    computed = await self._compute_outcome(
                        did, action, payload_json, created_iso,
                        now=now, oc=oc, bars=cached_bars)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad row never kills the sweep
                    log.warning("outcome compute failed", decision_id=did,
                                error=str(exc))
                    pending += 1
                    continue
                if computed is None:  # forward bars not there yet: retry later
                    pending += 1
                    continue
                resolution, candidate = computed
                resolutions.append((did, resolution))
                if candidate is not None:
                    candidates.append(candidate)

            # ---- PHASE 2: commit (lessons first, then markers) -------------
            ranked = sorted(candidates, key=lambda c: c[1], reverse=True)
            chosen = [c for c in ranked
                      if c[1] >= oc.min_abs_alpha][:oc.max_lessons_per_sweep]
            backend = self._get_backend()
            allow_llm = backend is not None and bool(chosen)
            if allow_llm and self._over_budget is not None and await self._over_budget():
                log.warning("monthly AI budget reached; skipping outcome lessons")
                allow_llm = False
            lessons = 0
            for outcome, _rank in chosen:
                if not allow_llm or backend is None or self._stopped:
                    break
                prose = await reflect_on_outcome(backend, outcome,
                                                 model=self._model, usage=usage)
                if prose is None:
                    # Refusal/failure: the marker below still commits — a
                    # refusing backend must not pin rows pending forever.
                    continue
                prefix = "hold" if outcome.kind == "hold" else "cf"
                lesson = TradeLesson(
                    id=f"{prefix}-{outcome.decision_id[:16]}",
                    kind=outcome.kind, symbol=outcome.symbol, strategy="",
                    decision_id=outcome.decision_id,
                    entered_at=outcome.decided_at, exited_at=now,
                    realized_return=outcome.forward_return, alpha=outcome.alpha,
                    holding_days=float(outcome.horizon_trading_days), lesson=prose,
                    model=getattr(backend, "model", self._model), created_at=now)
                await self._db.add_trade_lesson(lesson)
                await self._audit_append("ai", "lesson_written",
                                         {"id": lesson.id, "symbol": lesson.symbol,
                                          "kind": lesson.kind})
                lessons += 1
            resolved = 0
            for did, resolution in resolutions:
                if self._stopped:  # unmarked rows simply re-grade next sweep
                    break
                await self._db.mark_decision_resolved(
                    did, resolved_at_iso=now.isoformat(),
                    resolution_json=json.dumps(resolution, default=str))
                resolved += 1
            if resolved + lessons > 0:  # keep idle days quiet
                await self._audit_append("ai", "outcomes_resolved",
                                         {"resolved": resolved, "lessons": lessons,
                                          "pending": pending})
        except Exception as exc:  # never let the sweep break the scheduler
            log.warning("outcome sweep failed", error=str(exc))
        finally:
            # Meter spend even when the sweep failed mid-pipeline, so the
            # monthly budget is never silently under-counted.
            if usage and self._record_usage is not None:
                try:
                    await self._record_usage(sum_usage(usage))
                except Exception as exc:
                    log.warning("outcome usage metering failed", error=str(exc))

    async def _compute_outcome(
            self, did: str, action: str, payload_json: str, created_iso: str, *,
            now: datetime, oc: OutcomeResolutionConfig,
            bars: Callable[[str], Awaitable[list[Bar] | None]],
    ) -> tuple[dict[str, Any], tuple[DecisionOutcome, float] | None] | None:
        """(resolution JSON dict, optional (lesson candidate, rank)) for one
        pending decision — or None to leave it PENDING for a later sweep.

        Tolerant dict access over the stored payload on purpose: legacy rows
        vary and Decision.model_validate (extra='forbid') would reject them.
        """
        created_at = datetime.fromisoformat(created_iso)
        raw = json.loads(payload_json) if payload_json else {}
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        status_rows = await self._db.fetch_all(
            "SELECT status FROM orders WHERE decision_id = ?", (did,))
        counts: dict[str, int] = dict(Counter(str(r[0]) for r in status_rows))
        # Executed detection is STATUS-based, not any-orders-row: vetoed and
        # human-rejected orders ARE persisted with decision_id, and an any-row
        # check would mark them executed and never grade them. Any filled/
        # partially_filled order marks the decision executed in v1 (unfilled
        # legs of a partially-filled decision are not graded — the
        # closed-episode loop owns executed grading; the sweep only closes the
        # pending marker).
        if any(s in _EXECUTED_STATUSES for s in counts):
            return {"status": "executed", "orders": counts}, None
        aged_out = created_iso < (now - timedelta(days=oc.max_age_days)).isoformat()
        raw_trades = payload.get("trades")
        trades = raw_trades if isinstance(raw_trades, list) else []
        legs: list[tuple[str, str]] = []  # open-side (symbol, side), proposal order
        proposed = 0
        for t in trades:
            if not isinstance(t, dict):
                continue
            sym = str(t.get("symbol", "")).strip().upper()
            if not sym:
                continue
            proposed += 1
            side = str(t.get("side", ""))
            if side in _OPEN_SIDES:
                legs.append((sym, side))
        if proposed and not legs:
            # Exit management only (e.g. a vetoed close): grading it as a
            # missed OPEN would be wrong in both directions.
            return {"status": "unexecuted_exit_only", "orders": counts}, None
        if legs:
            return await self._grade_unexecuted(
                did, action, payload, legs, counts, created_at,
                oc=oc, bars=bars, aged_out=aged_out)
        return await self._grade_hold(
            did, action, created_at, oc=oc, bars=bars, aged_out=aged_out)

    async def _grade_unexecuted(
            self, did: str, action: str, payload: dict[str, Any],
            legs: list[tuple[str, str]], counts: dict[str, int],
            created_at: datetime, *, oc: OutcomeResolutionConfig,
            bars: Callable[[str], Awaitable[list[Bar] | None]], aged_out: bool,
    ) -> tuple[dict[str, Any], tuple[DecisionOutcome, float] | None] | None:
        if "rejected_risk" in counts:
            blocked = "rejected_risk"
        elif "rejected_human" in counts:
            blocked = "rejected_human"
        elif counts:
            blocked = "unfilled"
        else:  # never reached execution (research/dry cycle)
            blocked = ""
        seen: dict[str, str] = {}  # unique symbols in proposal order
        for sym, side in legs:
            if sym not in seen:
                if len(seen) >= _MAX_SYMBOLS_PER_DECISION:
                    break
                seen[sym] = side
        outcomes: list[dict[str, Any]] = []
        for sym, side in seen.items():
            fwd = forward_return((await bars(sym)) or [], created_at,
                                 oc.horizon_trading_days)
            if fwd is None:
                if aged_out:  # its bars will never come: terminal marker
                    return {"status": "unresolvable"}, None
                return None  # PENDING: skip and retry once bars extend
            signed = -fwd if side == "sell_to_open" else fwd
            bench_sym = self._benchmark_for(sym)
            bench_fwd = forward_return((await bars(bench_sym)) or [], created_at,
                                       oc.horizon_trading_days)
            alpha = None if bench_fwd is None else signed - bench_fwd
            outcomes.append({
                "symbol": sym, "side": side, "forward_return": round(signed, 6),
                "benchmark": bench_sym,
                "benchmark_return": None if bench_fwd is None else round(bench_fwd, 6),
                "alpha": None if alpha is None else round(alpha, 6)})
        resolution = {"status": "unexecuted",
                      "horizon_trading_days": oc.horizon_trading_days,
                      "outcomes": outcomes, "orders": counts}
        # Headline: max |alpha|, falling back to max |signed forward return|
        # when no benchmark leg resolved.
        with_alpha = [o for o in outcomes if o["alpha"] is not None]
        if with_alpha:
            headline = max(with_alpha, key=lambda o: abs(float(o["alpha"])))
            rank = abs(float(headline["alpha"]))
        else:
            headline = max(outcomes, key=lambda o: abs(float(o["forward_return"])))
            rank = abs(float(headline["forward_return"]))
        thesis, confidence, invalidation = _risk_case_from_payload(payload)
        outcome = DecisionOutcome(
            decision_id=did, kind="counterfactual", symbol=str(headline["symbol"]),
            action=action, side=str(headline["side"]), thesis=thesis,
            entry_confidence=confidence, invalidation=invalidation,
            blocked_status=blocked, decided_at=created_at,
            horizon_trading_days=oc.horizon_trading_days,
            forward_return=float(headline["forward_return"]),
            benchmark=str(headline["benchmark"]),
            benchmark_return=headline["benchmark_return"], alpha=headline["alpha"])
        return resolution, (outcome, rank)

    async def _grade_hold(
            self, did: str, action: str, created_at: datetime, *,
            oc: OutcomeResolutionConfig,
            bars: Callable[[str], Awaitable[list[Bar] | None]], aged_out: bool,
    ) -> tuple[dict[str, Any], tuple[DecisionOutcome, float] | None] | None:
        # The portfolio is graded against the EQUITY benchmark: a hold is a
        # book-level call, not an instrument call.
        bench_sym = self._benchmark_symbol
        bbars = (await bars(bench_sym)) or []
        bench_fwd = forward_return(bbars, created_at, oc.horizon_trading_days)
        if bench_fwd is None:
            if aged_out:
                return {"status": "unresolvable"}, None
            return None
        window = _forward_window(bbars, created_at, oc.horizon_trading_days)
        portfolio = (await self._portfolio_return(window[0], window[1])
                     if window is not None else None)
        alpha = None if portfolio is None else portfolio - bench_fwd
        resolution = {
            "status": "hold", "horizon_trading_days": oc.horizon_trading_days,
            "benchmark": bench_sym, "benchmark_return": round(bench_fwd, 6),
            "portfolio_return": None if portfolio is None else round(portfolio, 6),
            "alpha": None if alpha is None else round(alpha, 6)}
        if portfolio is None or alpha is None:
            return resolution, None  # marker only: no fabricated hold grade
        outcome = DecisionOutcome(
            decision_id=did, kind="hold", symbol="PORTFOLIO", action=action,
            decided_at=created_at, horizon_trading_days=oc.horizon_trading_days,
            forward_return=portfolio, benchmark=bench_sym,
            benchmark_return=bench_fwd, alpha=alpha)
        return resolution, (outcome, abs(alpha))

    async def _portfolio_return(self, t0: datetime, t1: datetime) -> float | None:
        """Best-effort account-scoped portfolio return between two mark
        timestamps (equity_marks asof probes). None when a probe is missing,
        both probes hit the SAME mark (an app-down window must not fake a flat
        book), a value fails to parse, or the base equity is not positive.
        Decimal until the final advisory ratio, like the sibling analytics.
        """
        scope = self._scope()

        async def probe(dt: datetime) -> tuple[Any, ...] | None:
            return await self._db.fetch_one(
                "SELECT at, equity FROM equity_marks WHERE broker = ? AND at <= ? "
                "ORDER BY at DESC LIMIT 1", (scope, dt.isoformat()))

        r0, r1 = await probe(t0), await probe(t1)
        if r0 is None or r1 is None or r0[0] == r1[0]:
            return None
        try:
            e0, e1 = Decimal(str(r0[1])), Decimal(str(r1[1]))
        except (InvalidOperation, ValueError):
            return None
        if e0 <= 0:
            return None
        return float(e1 / e0 - 1)

    # -- behavioral self-assessment (scheduled; deterministic, zero LLM) -------

    async def behavior_sweep(self) -> None:
        """Weekly deterministic bias-profile refresh over the account's own
        closed round trips. ONE kind='bias_profile' lesson per account scope
        (stable id ``bias:<scope>`` under INSERT OR REPLACE); silent below
        ``min_trades``; NO LLM ever (model='deterministic'). Never raises.
        """
        cfg = self._config
        if not (cfg.enabled and cfg.behavior.enabled) or self._stopped:
            return
        try:
            bc = cfg.behavior
            now = datetime.now(UTC)
            window_start = now - timedelta(days=bc.window_days)
            trips = [t for t in build_round_trips(await self._load_fills(None, None))
                     if t.exited_at >= window_start]
            if len(trips) < bc.min_trades:
                return
            # Daily bars for the most-traded symbols (ties alphabetical), for
            # runup coverage only — a failed fetch loses coverage, not the
            # profile.
            traded = sorted(Counter(t.symbol for t in trips).items(),
                            key=lambda kv: (-kv[1], kv[0]))
            bars_by_symbol: dict[str, list[Bar]] = {}
            for sym, _n in traded[:bc.max_bar_symbols]:
                try:
                    bars_by_symbol[sym] = await self._router.bars(
                        sym, timeframe="1d", limit=400)
                except Exception as exc:
                    log.warning("behavior bars fetch failed", symbol=sym,
                                error=str(exc))
            profile = compute_bias_profile(
                trips, bars_by_symbol, window_days=bc.window_days,
                min_trades=bc.min_trades, runup_days=bc.runup_days,
                runup_threshold=bc.runup_threshold, reentry_days=bc.reentry_days,
                now=now)
            if profile is None:
                return
            lesson = TradeLesson(
                # One row per account scope, refreshed in place weekly.
                id=f"bias:{self._scope() or 'default'}", kind="bias_profile",
                symbol="PORTFOLIO", strategy="", decision_id=None,
                entered_at=window_start, exited_at=now,
                # Mean trip return over the window + the window length: the
                # NOT-NULL float slots documented on TradeLesson.
                realized_return=sum(t.return_pct for t in trips) / len(trips),
                alpha=None, holding_days=float(bc.window_days),
                lesson=profile.render(600), model="deterministic", created_at=now)
            await self._db.add_trade_lesson(lesson)
            await self._audit_append("ai", "behavior_profile_written",
                                     {"id": lesson.id, "trades": profile.trades})
        except Exception as exc:  # never let the sweep break the scheduler
            log.warning("behavior sweep failed", error=str(exc))
