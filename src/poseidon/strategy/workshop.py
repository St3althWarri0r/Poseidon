"""Algorithm workshop: persistence and lifecycle for custom algorithms.

The workshop owns the ``algorithms`` table and the live wiring into the
strategy engine. States:

  * ``draft``    — saved, validated for syntax/contract, not running.
                   Everything Claude proposes lands here; only the
                   operator promotes it.
  * ``active``   — compiled and scanning each review cycle alongside the
                   built-in strategies (signals only — never orders).
  * ``archived`` — kept for reference, never runs.

Every state change is audited.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ..core.config import BacktestEvalConfig
from ..core.errors import ConfigError
from ..data.router import DataRouter
from ..portfolio.state import PortfolioState
from ..security.audit import AuditLog
from ..storage.db import Database
from .custom import CustomAlgorithm, validate_algorithm
from .engine import StrategyEngine

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

# The review note stamped on bundled example algorithms when first seeded.
# The dashboard's Dry Run panel uses it to recognise the built-in starters.
BUNDLED_REVIEW_NOTE = "bundled example — review before activating"

_COLUMNS = ("id, name, description, source, symbols, params, status, "
            "created_by, review_notes, sleeve_pct, created_at, updated_at")


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = [c.strip() for c in _COLUMNS.split(",")]
    record = dict(zip(keys, row, strict=True))
    record["symbols"] = json.loads(record["symbols"] or "[]")
    record["params"] = json.loads(record["params"] or "{}")
    return record


class AlgorithmWorkshop:
    def __init__(self, db: Database, engine: StrategyEngine, audit: AuditLog,
                 *, default_symbols: list[str],
                 sleeve_caps: dict[str, float] | None = None,
                 benchmark_symbol: str = "SPY",
                 eval_config: BacktestEvalConfig | None = None) -> None:
        self._db = db
        self._engine = engine
        self._audit = audit
        self._default_symbols = default_symbols
        # Shared with the risk engine: strategy name -> fraction of equity
        # that positions from that algorithm may occupy (its sleeve).
        self._sleeve_caps = sleeve_caps if sleeve_caps is not None else {}
        # Backtest evaluation: the benchmark reuses risk.benchmark_symbol
        # (no separate key) and the depth knobs come from config.backtest.
        self._benchmark_symbol = benchmark_symbol
        self._eval = eval_config or BacktestEvalConfig()

    # -- queries ---------------------------------------------------------------

    async def list_all(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM algorithms ORDER BY updated_at DESC"
        )
        return [_row_to_dict(r) for r in rows]

    async def get(self, algo_id: str) -> dict[str, Any]:
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM algorithms WHERE id = ?", (algo_id,)
        )
        if row is None:
            raise KeyError(f"unknown algorithm {algo_id}")
        return _row_to_dict(row)

    # -- lifecycle ---------------------------------------------------------------

    async def create(self, *, name: str, source: str, description: str = "",
                     symbols: list[str] | None = None, params: dict[str, Any] | None = None,
                     created_by: str = "user", review_notes: str = "",
                     sleeve_pct: float = 0.0) -> dict[str, Any]:
        name = self._clean_name(name)
        sleeve_pct = self._clean_sleeve(sleeve_pct)
        problems = validate_algorithm(source)
        if problems:
            raise ConfigError("algorithm failed validation: " + "; ".join(problems))
        now = datetime.now(UTC).isoformat()
        algo_id = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO algorithms (id, name, description, source, symbols, params, status, "
            "created_by, review_notes, sleeve_pct, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
            (algo_id, name, description, source,
             json.dumps([s.upper() for s in (symbols or [])]), json.dumps(params or {}),
             created_by, review_notes, sleeve_pct, now, now),
        )
        await self._audit.append(created_by if created_by == "claude" else "human",
                                 "algorithm.created", {"id": algo_id, "name": name})
        log.info("algorithm saved as draft", id=algo_id, name=name, by=created_by)
        return await self.get(algo_id)

    async def update(self, algo_id: str, *, source: str | None = None,
                     description: str | None = None, symbols: list[str] | None = None,
                     params: dict[str, Any] | None = None,
                     review_notes: str | None = None,
                     sleeve_pct: float | None = None) -> dict[str, Any]:
        record = await self.get(algo_id)
        if sleeve_pct is not None:
            record["sleeve_pct"] = self._clean_sleeve(float(sleeve_pct))
        if source is not None:
            problems = validate_algorithm(source)
            if problems:
                raise ConfigError("algorithm failed validation: " + "; ".join(problems))
            record["source"] = source
        if description is not None:
            record["description"] = description
        if symbols is not None:
            record["symbols"] = [s.upper() for s in symbols]
        if params is not None:
            record["params"] = params
        if review_notes is not None:
            record["review_notes"] = review_notes
        # Fail fast on a live edit: compile/exec the new source BEFORE
        # persisting so a broken edit (a module-level error that passes the
        # AST screen but raises at exec) leaves the running old source intact
        # rather than storing unrunnable source still marked 'active'.
        if source is not None and record["status"] == "active":
            try:
                CustomAlgorithm(
                    algo_name=record["name"], source=record["source"],
                    symbols=record["symbols"] or self._default_symbols,
                    options=record["params"],
                )
            except Exception as exc:
                raise ConfigError(f"edited algorithm does not compile/run: {exc}") from exc
        await self._db.execute(
            "UPDATE algorithms SET description=?, source=?, symbols=?, params=?, "
            "review_notes=?, sleeve_pct=?, updated_at=? WHERE id=?",
            (record["description"], record["source"], json.dumps(record["symbols"]),
             json.dumps(record["params"]), record["review_notes"], record["sleeve_pct"],
             datetime.now(UTC).isoformat(), algo_id),
        )
        await self._audit.append("human", "algorithm.updated", {"id": algo_id, "name": record["name"]})
        # An edited active algorithm hot-reloads so what runs is what you see.
        if record["status"] == "active":
            await self.activate(algo_id)
        return await self.get(algo_id)

    async def activate(self, algo_id: str) -> dict[str, Any]:
        record = await self.get(algo_id)
        strategy = CustomAlgorithm(
            algo_name=record["name"], source=record["source"],
            symbols=record["symbols"] or self._default_symbols,
            options=record["params"],
        )
        self._engine.add_strategy(strategy)
        if record["sleeve_pct"] > 0:
            self._sleeve_caps[f"algo:{record['name']}"] = float(record["sleeve_pct"])
        else:
            self._sleeve_caps.pop(f"algo:{record['name']}", None)
        await self._set_status(algo_id, "active")
        await self._audit.append("human", "algorithm.activated",
                                 {"id": algo_id, "name": record["name"]})
        log.info("algorithm activated", name=record["name"])
        return await self.get(algo_id)

    async def deactivate(self, algo_id: str, *, archive: bool = False,
                         actor: str = "human") -> dict[str, Any]:
        record = await self.get(algo_id)
        self._engine.remove_strategy(f"algo:{record['name']}")
        self._sleeve_caps.pop(f"algo:{record['name']}", None)
        await self._set_status(algo_id, "archived" if archive else "draft")
        await self._audit.append(actor, "algorithm.deactivated",
                                 {"id": algo_id, "name": record["name"]})
        return await self.get(algo_id)

    async def delete(self, algo_id: str) -> None:
        record = await self.get(algo_id)
        self._engine.remove_strategy(f"algo:{record['name']}")
        self._sleeve_caps.pop(f"algo:{record['name']}", None)
        await self._db.execute("DELETE FROM algorithms WHERE id = ?", (algo_id,))
        await self._audit.append("human", "algorithm.deleted",
                                 {"id": algo_id, "name": record["name"]})

    async def test_run(self, algo_id: str, router: DataRouter,
                       portfolio: PortfolioState) -> dict[str, Any]:
        """Dry run: compile the CURRENT saved source and scan once against
        live data. Nothing is saved, activated, or traded — the returned
        signals are exactly what a review cycle would receive."""
        record = await self.get(algo_id)
        strategy = CustomAlgorithm(
            algo_name=record["name"], source=record["source"],
            symbols=record["symbols"] or self._default_symbols,
            options=record["params"],
        )
        signals = await strategy.scan(router, portfolio)
        return {
            "algorithm": record["name"],
            "signals": [s.as_dict() for s in signals],
            "count": len(signals),
            "note": "dry run against live data — nothing was traded or saved",
        }

    async def _resolve_risk_free(self, as_of: date | None) -> float:
        """Annualized risk-free rate for a backtest window.

        ``auto`` (the default) reads the Treasury 3-month par yield in effect
        at the window's end; any number is taken literally, and 0.0 explicitly
        opts out. Every failure degrades to 0.0 with a warning rather than
        taking the run down — the workshop's standing contract is that a
        missing input becomes an explicit null, never an exception. The degrade
        is logged because it silently restores the overstated rf=0 Sharpe.
        """
        configured = self._eval.risk_free_annual
        if configured != "auto":
            return float(configured)
        day: date = as_of or datetime.now(UTC).date()
        try:
            from ..data.treasury import fetch_yield_curve, risk_free_annual_on

            rows = await fetch_yield_curve(year=day.year)
            rate = risk_free_annual_on(rows, day)
            if rate == 0.0:
                # A window ending in the first days of January precedes that
                # year's first publication; the prior year holds the curve.
                rows = await fetch_yield_curve(year=day.year - 1)
                rate = risk_free_annual_on(rows, day)
        except Exception as exc:
            log.warning("risk-free rate unavailable; Sharpe/Sortino revert to rf=0",
                        error=str(exc))
            return 0.0
        if rate == 0.0:
            log.warning("no Treasury curve on or before the window end; using rf=0",
                        as_of=str(day))
        return rate

    async def backtest(self, algo_id: str, router: DataRouter, portfolio: PortfolioState,
                       *, years: int = 5, starting_cash: float = 100_000.0,
                       period: str | None = None, start: str | None = None,
                       end: str | None = None) -> dict[str, Any]:
        """Backtest the CURRENT saved source against real historical daily
        bars. A live discovery scan first records every symbol the
        algorithm actually touches (rotation trees fetch far beyond their
        configured universe); full history is then pulled for each and the
        code replays through the anti-lookahead window. Symbols whose
        history cannot be fetched are reported, never silently invented."""
        from datetime import date, timedelta

        from ..backtest.rebalance import rebalance_backtest
        from ..core.errors import DataError

        # Resolve the lookback window. period wins over years; explicit
        # dates win over both ("custom").
        today = date.today()
        window_start: date | None = None
        window_end: date | None = None
        if start or (period or "").lower() == "custom":
            if not start:
                raise ValueError("custom period needs a start date (YYYY-MM-DD)")
            window_start = date.fromisoformat(start)
            window_end = date.fromisoformat(end) if end else None
        elif period:
            key = period.lower()
            if key == "ytd":
                window_start = date(today.year, 1, 1)
            elif key in ("1y", "3y", "5y", "10y"):
                window_start = today - timedelta(days=365 * int(key[:-1]))
            else:
                raise ValueError(f"unknown period '{period}' — use ytd/1y/3y/5y/10y/custom")
        if window_start is not None:
            if window_start >= today:
                raise ValueError("start date must be in the past")
            # Trading days from start to today, plus the 200-day warmup.
            span_days = (today - window_start).days
            years = 0  # sentinel; limit computed below
            fetch_limit = min(int(span_days * 252 / 365) + 340, 10 * 252 + 340)
        else:
            fetch_limit = min(max(years, 1), 10) * 252 + 320

        record = await self.get(algo_id)
        symbols = record["symbols"] or self._default_symbols

        touched: set[str] = {s.upper() for s in symbols}

        class _RecordingRouter:
            """Pass-through to the live router that records bar requests."""

            def __init__(self, inner: DataRouter) -> None:
                self._inner = inner

            async def bars(self, symbol: str, **kwargs: Any) -> Any:
                touched.add(symbol.upper())
                return await self._inner.bars(symbol, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        algo = CustomAlgorithm(algo_name=record["name"], source=record["source"],
                               symbols=symbols, options=record["params"])
        await algo.scan(_RecordingRouter(router), portfolio)  # type: ignore[arg-type]

        history, skipped = {}, []
        for symbol in sorted(touched):
            try:
                bars = await router.bars(symbol, timeframe="1d", limit=fetch_limit)
            except DataError:
                bars = []
            if len(bars) >= 60:
                history[symbol] = bars
            else:
                skipped.append(symbol)
        if not history:
            raise ValueError("no historical bars available for any symbol the algorithm uses")

        # Benchmark closes: reuse the algorithm's own fetch when it already
        # touched the symbol, otherwise fetch once. Under 60 bars the
        # rebalance layer degrades to the reported equal-weight fallback.
        bench_symbol = self._benchmark_symbol.upper()
        bench_bars = history.get(bench_symbol)
        bench_fetched = False
        if bench_bars is None:
            try:
                bench_bars = await router.bars(bench_symbol, timeframe="1d",
                                               limit=fetch_limit)
                bench_fetched = True
            except DataError:
                bench_bars = []
        benchmark: tuple[str, dict[date, float]] | None = None
        if len(bench_bars) >= 60:
            benchmark = (bench_symbol,
                         {b.start.date(): float(b.close) for b in bench_bars})

        report = await rebalance_backtest(
            algo, history, starting_cash=starting_cash,
            start=window_start, end=window_end, benchmark=benchmark,
            risk_free_annual=await self._resolve_risk_free(window_end),
            significance_runs=self._eval.significance_runs,
            bootstrap_runs=self._eval.bootstrap_runs,
            monte_carlo_runs=self._eval.monte_carlo_runs,
            seed=self._eval.seed,
        )
        if self._eval.walk_forward_folds > 0:
            from ..backtest.analysis import walk_forward_rebalance

            report["walk_forward"] = await walk_forward_rebalance(
                lambda: CustomAlgorithm(algo_name=record["name"], source=record["source"],
                                        symbols=symbols, options=record["params"]),
                history, folds=self._eval.walk_forward_folds,
                starting_cash=starting_cash, start=window_start, end=window_end)
        else:
            report["walk_forward"] = None
        report["algorithm"] = record["name"]
        report["symbols_tested"] = sorted(history)
        report["symbols_skipped_no_history"] = skipped

        # Run card: canonical-JSON hashes (audit.py form) of the effective
        # config, the exact source tested, and the sanitized result.
        from ..backtest.stats import sanitize_json

        effective_params = {
            "starting_cash": starting_cash,
            "slippage_pct": 0.0005,
            "commission_per_trade": 0.0,
            "years": years,
            "period": period,
            "start": start,
            "end": end,
            "benchmark_symbol": bench_symbol,
            "seed": self._eval.seed,
            "significance_runs": self._eval.significance_runs,
            "bootstrap_runs": self._eval.bootstrap_runs,
            "monte_carlo_runs": self._eval.monte_carlo_runs,
            "walk_forward_folds": self._eval.walk_forward_folds,
            "symbols_tested": sorted(history),
        }
        canonical_params = json.dumps(effective_params, sort_keys=True,
                                      separators=(",", ":"), default=str)
        config_hash = hashlib.sha256(canonical_params.encode("utf-8")).hexdigest()
        source_sha256 = hashlib.sha256(record["source"].encode("utf-8")).hexdigest()
        sources = {bars[0].source for bars in history.values() if bars}
        if benchmark is not None and bench_fetched and bench_bars:
            sources.add(bench_bars[0].source)

        sanitized_report, nulled = sanitize_json(report)
        report = dict(sanitized_report)
        run_warnings = list(report.pop("warnings", []) or [])
        if nulled:
            run_warnings.append(f"non_finite_values_nulled:{nulled}")
        result_hash = hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":"),
                       default=str).encode("utf-8")
        ).hexdigest()
        run_id = uuid.uuid4().hex[:12]
        bench_block = report.get("benchmark") or {}
        report["run_card"] = {
            "schema_version": 1,
            "run_id": run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "engine": "rebalance_daily_close",
            "config_hash": config_hash,
            "source_sha256": source_sha256,
            "result_hash": result_hash,
            "benchmark_source": bench_block.get("source"),
            "data_sources": sorted(sources),
            "seed": self._eval.seed,
            "warnings": sorted(set(run_warnings)),
        }
        # Audit purity (invariant 4): identifiers, sha256 digests, and one
        # numeric metric — warnings and data sources stay OUT of the chain.
        await self._audit.append("human", "algorithm.backtested",
                                 {"id": algo_id, "name": record["name"],
                                  "run_id": run_id, "config_hash": config_hash,
                                  "source_sha256": source_sha256,
                                  "result_hash": result_hash,
                                  "total_return": report["total_return"]})
        return report

    async def seed_bundled(self, directory: Any) -> int:
        """First boot: load the bundled example algorithms (the operator's
        Composer ports and the intraday day trader) into the library as
        drafts. Runs exactly once per database — deletions and edits are
        never overwritten afterwards."""
        from pathlib import Path

        directory = Path(directory)
        if await self._db.kv_get("workshop_seeded") or not directory.is_dir():
            return 0
        count = 0
        for path in sorted(directory.glob("*.py")):
            name = self._clean_name(path.stem)
            exists = await self._db.fetch_one(
                "SELECT 1 FROM algorithms WHERE name = ?", (name,)
            )
            if exists:
                continue
            source = path.read_text(encoding="utf-8")
            first_line = source.lstrip().splitlines()[0].lstrip("# ").strip()
            try:
                await self.create(name=path.stem, source=source,
                                  description=first_line,
                                  review_notes=BUNDLED_REVIEW_NOTE)
                count += 1
            except ConfigError as exc:
                log.error("bundled algorithm failed validation; skipped",
                          file=path.name, error=str(exc))
        await self._db.kv_set("workshop_seeded", True)
        if count:
            log.info("bundled algorithms seeded as drafts", count=count)
        return count

    async def load_active(self) -> int:
        """Startup: compile and register every active algorithm. One broken
        algorithm demotes itself to draft (with a note) instead of blocking
        boot — the platform must come up."""
        count = 0
        for record in await self.list_all():
            if record["status"] != "active":
                continue
            try:
                strategy = CustomAlgorithm(
                    algo_name=record["name"], source=record["source"],
                    symbols=record["symbols"] or self._default_symbols,
                    options=record["params"],
                )
            except Exception as exc:
                # Any failure (validation, compile, or exec-time) demotes to
                # draft rather than blocking boot — the platform must come up.
                log.error("active algorithm failed to compile; demoted to draft",
                          name=record["name"], error=str(exc))
                await self._set_status(record["id"], "draft")
                await self._db.execute(
                    "UPDATE algorithms SET review_notes = ? WHERE id = ?",
                    (f"demoted at startup: {exc}", record["id"]),
                )
                continue
            self._engine.add_strategy(strategy)
            if record["sleeve_pct"] > 0:
                self._sleeve_caps[f"algo:{record['name']}"] = float(record["sleeve_pct"])
            count += 1
        if count:
            log.info("workshop algorithms loaded", active=count)
        return count

    # -- internals ---------------------------------------------------------------

    async def _set_status(self, algo_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE algorithms SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now(UTC).isoformat(), algo_id),
        )

    @staticmethod
    def _clean_sleeve(value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ConfigError("sleeve_pct must be between 0 (no sleeve) and 1")
        return round(float(value), 4)

    @staticmethod
    def _clean_name(name: str) -> str:
        cleaned = "".join(c for c in name.strip().lower().replace(" ", "_")
                          if c.isalnum() or c in "_-")[:48]
        if not cleaned:
            raise ConfigError("algorithm name must contain letters or digits")
        return cleaned
