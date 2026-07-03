"""User/AI-authored algorithms: the workshop's execution layer.

An algorithm is Python source stored in the database that defines one
entry point::

    async def scan(ctx) -> list[dict]:
        # ctx.quote(symbol) / ctx.bars(symbol, timeframe="1d", limit=100)
        # ctx.option_chain(symbol) — all live, freshness-enforced
        # ctx.symbols, ctx.params, ctx.positions, ctx.equity, ctx.log(msg)
        return [{"symbol": "AAPL", "direction": "long", "strength": 0.7,
                 "evidence": {"why": "..."}}]

Algorithms are *screeners with the same standing as built-in strategies*:
their signals feed the AI review cycle as candidates — they can never
place an order directly, so an algorithm bug cannot trade by itself.

Trust model: this executes operator-approved code in-process, exactly
like an installed plugin. Validation includes a lint-level screen that
rejects imports/builtins with no business in a screener (os, subprocess,
open, exec, ...) to catch accidents and force review of pasted code —
it is a guardrail, not a sandbox. Activation is the trust decision, and
only the operator can activate (AI-authored algorithms are saved as
drafts).
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from ..core.errors import DataError
from ..data.router import DataRouter
from ..portfolio.state import PortfolioState
from . import indicators
from .base import Signal, Strategy

log = structlog.get_logger(__name__)

_FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "shutil", "socket", "ctypes", "importlib",
    "multiprocessing", "threading", "pickle", "marshal", "signal", "pty",
    "http", "urllib", "requests", "httpx", "aiohttp", "ftplib", "smtplib",
    "pathlib", "tempfile", "webbrowser",
}
_FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "exit", "quit"}
_ALLOWED_DIRECTIONS = {"long", "short", "exit", "hedge", "income"}
MAX_SOURCE_BYTES = 64_000


def validate_algorithm(source: str) -> list[str]:
    """Static validation: returns a list of problems (empty = passes).
    Catches syntax errors, a missing/incorrect entry point, and
    imports/calls that have no place in a market screener."""
    problems: list[str] = []
    if not source.strip():
        return ["source is empty"]
    if len(source.encode()) > MAX_SOURCE_BYTES:
        return [f"source exceeds {MAX_SOURCE_BYTES // 1000}kB"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: line {exc.lineno}: {exc.msg}"]

    scan_def: ast.AsyncFunctionDef | None = None
    for stmt in tree.body:
        if isinstance(stmt, ast.AsyncFunctionDef) and stmt.name == "scan":
            scan_def = stmt
        elif isinstance(stmt, ast.FunctionDef) and stmt.name == "scan":
            problems.append("`scan` must be `async def scan(ctx)` (it awaits live data)")
    if scan_def is None and not any("scan" in p for p in problems):
        problems.append("no `async def scan(ctx)` found — that is the required entry point")
    elif scan_def is not None and len(scan_def.args.args) != 1:
        problems.append("`scan` must take exactly one argument (ctx)")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_IMPORTS:
                    problems.append(f"import of '{alias.name}' is not allowed in an algorithm")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _FORBIDDEN_IMPORTS:
                problems.append(f"import from '{node.module}' is not allowed in an algorithm")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _FORBIDDEN_CALLS:
                problems.append(f"call to '{fn.id}()' is not allowed in an algorithm")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            problems.append(f"dunder attribute access ('{node.attr}') is not allowed")
    return sorted(set(problems))


@dataclass
class AlgoContext:
    """The surface an algorithm sees: live data plus a read-only view of
    the portfolio. No order methods exist here by design."""

    router: DataRouter
    portfolio: PortfolioState
    symbols: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    algo_name: str = ""

    async def quote(self, symbol: str) -> Any:
        return await self.router.quote(symbol, allow_delayed=True)

    async def bars(self, symbol: str, *, timeframe: str = "1d", limit: int = 100) -> list[Any]:
        return await self.router.bars(symbol, timeframe=timeframe, limit=limit)

    async def option_chain(self, symbol: str) -> Any:
        return await self.router.option_chain(symbol, allow_delayed=True)

    @property
    def positions(self) -> list[dict[str, Any]]:
        return [p.model_dump(mode="json") for p in self.portfolio.positions]

    @property
    def equity(self) -> float:
        return float(self.portfolio.equity or 0)

    def log(self, message: str) -> None:
        log.info("algo log", algo=self.algo_name, message=str(message)[:400])

    # Indicator primitives (Composer-compatible; see strategy/indicators.py).
    @staticmethod
    def rsi(closes: list[float], window: int = 14) -> float | None:
        return indicators.rsi(closes, window)

    @staticmethod
    def sma(closes: list[float], window: int) -> float | None:
        return indicators.sma(closes, window)

    @staticmethod
    def cumulative_return(closes: list[float], window: int) -> float | None:
        return indicators.cumulative_return(closes, window)

    @staticmethod
    def moving_average_return(closes: list[float], window: int) -> float | None:
        return indicators.moving_average_return(closes, window)


class CustomAlgorithm(Strategy):
    """Wraps stored source as a Strategy. ``name`` is prefixed ``algo:`` so
    signals are attributable to the workshop in every downstream view."""

    def __init__(self, *, algo_name: str, source: str, symbols: list[str],
                 options: dict[str, Any] | None = None) -> None:
        super().__init__(symbols=symbols, options=options)
        self.name = f"algo:{algo_name}"
        self.description = "workshop algorithm"
        self._algo_name = algo_name
        problems = validate_algorithm(source)
        if problems:
            raise ValueError("; ".join(problems))
        namespace: dict[str, Any] = {"__name__": f"poseidon_algo_{algo_name}"}
        exec(compile(source, f"<algo:{algo_name}>", "exec"), namespace)  # noqa: S102 — operator-approved code, see module docstring
        self._scan_fn = namespace["scan"]

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        ctx = AlgoContext(router=router, portfolio=portfolio, symbols=list(self.symbols),
                          params=dict(self.options), algo_name=self._algo_name)
        try:
            raw = await asyncio.wait_for(self._scan_fn(ctx), timeout=60)
        except (DataError, TimeoutError) as exc:
            log.warning("algorithm scan failed", algo=self.name, error=str(exc))
            return []
        return self._coerce_signals(raw)

    def _coerce_signals(self, raw: Any) -> list[Signal]:
        """Algorithm output is untrusted: validate every row, drop garbage
        loudly, clamp strength into [0, 1]."""
        signals: list[Signal] = []
        if not isinstance(raw, list):
            log.warning("algorithm returned non-list; ignoring", algo=self.name, type=type(raw).__name__)
            return []
        for row in raw[:50]:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper().strip()
            direction = str(row.get("direction", "")).lower().strip()
            if not symbol or direction not in _ALLOWED_DIRECTIONS:
                log.warning("algorithm signal dropped", algo=self.name, row=str(row)[:120])
                continue
            try:
                strength = min(max(float(row.get("strength", 0.5)), 0.0), 1.0)
            except (TypeError, ValueError):
                strength = 0.5
            raw_evidence = row.get("evidence")
            evidence_items = list(raw_evidence.items())[:12] if isinstance(raw_evidence, dict) else []
            evidence = {str(k)[:40]: _json_safe(v) for k, v in evidence_items}
            signals.append(Signal(strategy=self.name, symbol=symbol,
                                  direction=direction, strength=strength, evidence=evidence))
        return signals


def _json_safe(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return str(value)
    return str(value)[:200]
