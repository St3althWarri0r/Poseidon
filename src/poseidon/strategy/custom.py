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

Trust model: two layers. (1) A static lint-level screen (validate_algorithm)
rejects imports/builtins/indirect-call tricks with no business in a screener
(os, subprocess, open, exec, __builtins__, subscript-calls, dunder attribute
access, and format-string field traversal, ...) — friendly, early feedback.
(2) A *restricted* ``__builtins__`` (curated safe builtins + a guarded
``__import__`` that admits only pure-computation stdlib modules) blocks the
obvious filesystem/network/exec primitives at runtime.

These layers stop imports and forbidden-builtin calls, but the in-process
restricted-builtins sandbox is NOT a complete boundary: attribute/object-graph
traversal (e.g. via a ``str.format`` template like ``"{0.__class__}"``) can
still read reachable module globals without importing or calling anything. The
static screen catches the ``str.format``/``str.format_map`` field-traversal
class specifically — both literal templates and ones assembled at runtime
(concatenation / ``chr``) — but it is a lint-level guardrail, not a proof:
reflective reads outside that pattern remain possible, and true isolation
would require out-of-process execution. This matters because AI-authored
drafts can be test-run/backtested by the operator *before* activation, so
untrusted code runs even before approval. Activation is the
trust decision for letting an algorithm's signals feed live review cycles,
and only the operator can activate.
"""

from __future__ import annotations

import ast
import asyncio
import string
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
    "pathlib", "tempfile", "webbrowser", "builtins", "gc", "inspect",
    "code", "codeop", "runpy",
}
_FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "input",
                    "breakpoint", "exit", "quit", "getattr", "setattr", "delattr",
                    "globals", "vars", "locals", "memoryview"}
_ALLOWED_DIRECTIONS = {"long", "short", "exit", "hedge", "income"}
MAX_SOURCE_BYTES = 64_000

# Runtime sandbox. The static validator is a guardrail; this is the actual
# security boundary. Algorithm code is exec'd with a curated ``__builtins__``
# so that even a validator bypass (indirect call, ``__builtins__`` lookup,
# etc.) cannot reach a filesystem/network/exec primitive. Builtins that can
# escape the sandbox are removed; imports are routed through a guarded
# ``__import__`` that only admits pure-computation stdlib modules.
_UNSAFE_BUILTINS = {
    "open", "exec", "eval", "compile", "input", "breakpoint", "exit", "quit",
    "help", "getattr", "setattr", "delattr", "globals", "vars", "locals",
    "memoryview", "copyright", "credits", "license",
}
_SAFE_IMPORT_MODULES = {
    "math", "statistics", "cmath", "decimal", "fractions", "numbers", "random",
    "datetime", "itertools", "functools", "collections", "json", "re", "string",
    "typing", "dataclasses", "enum", "bisect", "heapq", "operator", "textwrap",
}


def _guarded_import(name: str, globals: Any = None, locals: Any = None,  # noqa: A002
                    fromlist: tuple[str, ...] = (), level: int = 0) -> Any:
    """The only importer an algorithm sees. Admits pure-computation stdlib
    modules by an allowlist; everything else (os, socket, importlib, relative
    imports, ...) raises, so an import can never reach the host."""
    root = name.split(".")[0]
    if level != 0 or root not in _SAFE_IMPORT_MODULES:
        raise ImportError(f"import of '{name}' is not allowed in an algorithm")
    return __import__(name, globals, locals, fromlist, level)


def _safe_builtins() -> dict[str, Any]:
    import builtins as _builtins

    safe = {
        name: getattr(_builtins, name)
        for name in dir(_builtins)
        if not name.startswith("_") and name not in _UNSAFE_BUILTINS
    }
    # ``__build_class__`` is needed for `class` statements; ``__import__`` is
    # the guarded importer. No other dunders are exposed.
    safe["__build_class__"] = _builtins.__build_class__
    safe["__import__"] = _guarded_import
    return safe


def _has_format_traversal(s: str) -> bool:
    """True if a str.format/format_map template does attribute or index
    traversal ('{0.__class__}' / '{0[key]}'), reaching an object graph the
    AST screen cannot see. Benign fields ('{}', '{0}', '{name}', '{x:.2f}')
    are allowed — only the field_name is inspected, so a dot in a format spec
    ('{px:.2f}') does not trip it. Malformed templates are ignored (a real
    str.format on them would raise)."""
    try:
        return any(
            fn is not None and ("." in fn or "[" in fn)
            for _, fn, _, _ in string.Formatter().parse(s)
        )
    except ValueError:
        return False


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
            # Bare name call: eval(...), open(...)
            if isinstance(fn, ast.Name) and fn.id in _FORBIDDEN_CALLS:
                problems.append(f"call to '{fn.id}()' is not allowed in an algorithm")
            # Attribute-form call: builtins.eval(...), x.getattr(...) — the
            # attribute name itself must not be a forbidden builtin.
            elif isinstance(fn, ast.Attribute) and fn.attr in _FORBIDDEN_CALLS:
                problems.append(f"call to '.{fn.attr}()' is not allowed in an algorithm")
            # str.format/format_map on a template the static screen cannot read as a
            # literal (a name, a concatenation, a chr()-assembled string) can do the
            # same '{0.__func__.__globals__}' field traversal at runtime that the
            # literal-constant screen below only catches for plain string literals.
            elif (isinstance(fn, ast.Attribute) and fn.attr in {"format", "format_map"}
                  and not (isinstance(fn.value, ast.Constant)
                           and isinstance(fn.value.value, str))):
                problems.append(
                    f"'.{fn.attr}()' on a non-literal template is not allowed — a "
                    "runtime-assembled format string can traverse the object graph "
                    "the static screen cannot read"
                )
            # Indirect call through subscription: ([open][0])(...),
            # __builtins__['__import__'](...) — a classic validator bypass.
            elif isinstance(fn, ast.Subscript):
                problems.append("indirect calls through subscription are not allowed in an algorithm")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            problems.append(f"dunder attribute access ('{node.attr}') is not allowed")
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            problems.append("access to '__builtins__' is not allowed in an algorithm")
        # A str.format/format_map template does attribute/index traversal in a
        # plain string ('{0.__class__.__globals__}'), which the AST screen for
        # ast.Attribute cannot see — a known validator bypass to module globals.
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and _has_format_traversal(node.value):
            problems.append(
                "format-string field access ('{...}.attr' / '{...}[key]') is not "
                "allowed — it bypasses the sandbox"
            )
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

    @property
    def ta(self) -> Any:  # module namespace; functions typed in indicators.py
        """The full indicator suite (strategy/indicators.py): sma, ema, rsi,
        macd, bollinger, stochastic, atr, adx, obv, cumulative_return,
        moving_average_return, stdev_price, stdev_return, max_drawdown,
        rate_of_change, highest, lowest."""
        return indicators

    # Shorthand for the most common primitives (Composer-compatible).
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
        # Restricted builtins are the security boundary: even if the static
        # validator is bypassed, the exec'd code cannot reach open/exec/eval/
        # __import__(unsafe) because they are simply absent from this namespace
        # (see _safe_builtins / _guarded_import).
        namespace: dict[str, Any] = {
            "__name__": f"poseidon_algo_{algo_name}",
            "__builtins__": _safe_builtins(),
        }
        exec(compile(source, f"<algo:{algo_name}>", "exec"), namespace)  # noqa: S102 — sandboxed builtins, see module docstring
        scan_fn = namespace.get("scan")
        if not callable(scan_fn):
            raise ValueError("algorithm defines no callable `scan`")
        self._scan_fn = scan_fn

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
