"""Task 8 wiring + isolation sweep for the verified-snapshot feature
(docs/superpowers/specs/2026-07-16-verified-snapshot-design.md §5, §6.8).

1. The snapshot config actually threads through: every ``ToolDispatcher`` the
   kernel builds (PM cycle AND chat) and the ``AnalysisService`` receive a
   ``snapshot_config=`` argument, so ``build_snapshot``/``get_market_snapshot``
   honour ``ai.snapshot.*`` rather than silently falling back to defaults.
2. Safety invariant #1 (advisory-only upstream): the snapshot builder and the
   ``InstrumentProfile`` identity model flow ONLY into advisory surfaces — no
   module under ``poseidon.risk`` or ``poseidon.execution`` may import either,
   so a resolved-identity string or a computed indicator can never reach the
   risk engine or the order path.

Both are enforced statically over the AST of real source (no imports executed),
and each net is proven to bite via a by-construction check.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import poseidon
from poseidon.app import ApplicationKernel

_SRC_ROOT = Path(poseidon.__file__).parent

# The snapshot builder module and the identity model symbol — the two things
# that must never appear in a risk/execution import.
_SNAPSHOT_MODULE = "poseidon.ai.analysis.snapshot"
_IDENTITY_SYMBOL = "poseidon.core.models.InstrumentProfile"

# Live packages whose import graph must stay clear of the snapshot surface.
# ``poseidon.portfolio`` is named alongside risk/execution in spec §5.1 and is
# clean today — guarding it now keeps a future accounting change from splicing the
# advisory identity/indicator surface into the live-state path.
_GUARDED_PACKAGES = ("poseidon.risk", "poseidon.execution", "poseidon.portfolio")


# ------------------------------------------------- config threads to the sites


def _constructor_keywords(source: str, ctor: str) -> list[list[str]]:
    """Keyword-argument names of every ``ctor(...)`` call in ``source``."""
    calls: list[list[str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name == ctor:
            calls.append([kw.arg for kw in node.keywords if kw.arg is not None])
    return calls


def test_dispatchers_and_analysis_service_get_snapshot_config() -> None:
    source = inspect.getsource(ApplicationKernel)

    dispatchers = _constructor_keywords(source, "ToolDispatcher")
    # PM cycle dispatcher + the chat dispatcher — both, not just one.
    assert len(dispatchers) == 2, (
        f"expected two ToolDispatcher sites, found {len(dispatchers)}")
    for kwargs in dispatchers:
        assert "snapshot_config" in kwargs, (
            "a ToolDispatcher site does not receive snapshot_config")

    analysis = _constructor_keywords(source, "AnalysisService")
    assert len(analysis) == 1, (
        f"expected one AnalysisService site, found {len(analysis)}")
    assert "snapshot_config" in analysis[0], (
        "AnalysisService does not receive snapshot_config")


# ------------------------------------------------- isolation: static AST scan


def _iter_source_modules() -> list[tuple[str, bool, str]]:
    """(dotted module name, is_package, source) for every module in src/poseidon."""
    modules: list[tuple[str, bool, str]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_SRC_ROOT).with_suffix("")
        parts = ("poseidon", *rel.parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        modules.append((".".join(parts), is_package, path.read_text(encoding="utf-8")))
    return modules


def _import_targets(module_name: str, source: str, *, is_package: bool = False) -> list[str]:
    """Absolute dotted targets of every import, relative imports resolved
    against ``module_name``. ``from X import a`` yields ``X.a`` so a symbol
    import and a submodule import are caught by the same prefix/equality test."""
    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = module_name.split(".")
                if not is_package:
                    parts = parts[:-1]
                parts = parts[: len(parts) - (node.level - 1)]
            else:
                parts = []
            if node.module:
                parts = [*parts, *node.module.split(".")]
            base = ".".join(parts)
            targets.extend(f"{base}.{alias.name}" for alias in node.names)
    return targets


def _reaches(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def _snapshot_surface_imports(module_name: str, source: str, *,
                              is_package: bool = False) -> list[str]:
    """Import targets of a module that touch the snapshot builder module or the
    ``InstrumentProfile`` identity symbol."""
    return [
        t for t in _import_targets(module_name, source, is_package=is_package)
        if _reaches(t, _SNAPSHOT_MODULE) or t == _IDENTITY_SYMBOL
    ]


def test_snapshot_and_profile_never_imported_by_risk_or_execution() -> None:
    checked = 0
    offenders: dict[str, list[str]] = {}
    for name, is_pkg, src in _iter_source_modules():
        if not any(_reaches(name, pkg) for pkg in _GUARDED_PACKAGES):
            continue
        checked += 1
        hits = _snapshot_surface_imports(name, src, is_package=is_pkg)
        if hits:
            offenders[name] = hits
    # engine, rules, circuit, manager, approvals, guardian, + the two __init__s.
    assert checked >= 6
    assert offenders == {}, (
        f"risk/execution must not import the snapshot surface; violations: {offenders}")


def test_isolation_net_flags_a_snapshot_or_profile_import() -> None:
    # Each shape splices the advisory snapshot surface into the live path; the
    # net must flag every one, directly or lazily, and stay quiet on the frozen
    # market models risk/execution legitimately import.
    for snippet in (
        "from ..ai.analysis.snapshot import build_snapshot\n",
        "from poseidon.ai.analysis.snapshot import Snapshot\n",
        "import poseidon.ai.analysis.snapshot\n",
        "from ..core.models import InstrumentProfile\n",
        "from poseidon.core.models import InstrumentProfile, Quote\n",
        "def f():\n    from ..ai.analysis import snapshot\n",  # lazy in-function
    ):
        assert _snapshot_surface_imports("poseidon.risk.engine", snippet), snippet
    # Legitimate frozen-model imports and a lookalike name stay clean.
    ok = ("from ..core.models import Quote, Bar, Position\n"
          "import poseidon.ai.analysis.snapshotter\n")
    assert _snapshot_surface_imports("poseidon.risk.engine", ok) == []
