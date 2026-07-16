"""Safety invariant 4: the research package is an offline lab, severed from
live trading in BOTH directions of the import graph:

1. No live module imports ``poseidon.research`` — only the CLI entry point
   (``poseidon research factors``) may reach it, so factor scores can never
   silently feed the agent prompt, risk engine, or order path.
2. ``poseidon.research`` imports nothing from the live side — no risk /
   execution / broker / ai modules; only its own modules, the frozen market
   models, pure indicator math, structlog, and the stdlib.

Enforced statically over the AST of every source file (no imports executed,
lazy in-function imports included). The checkers are plain functions over
(module_name, source) so the by-construction tests below prove each net
actually catches the violation class it exists for.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import poseidon

_SRC_ROOT = Path(poseidon.__file__).parent

# The only sanctioned consumer: the offline `poseidon research factors` command.
_ALLOWED_CONSUMERS = {"poseidon.cli"}

# Everything research/ may import from inside the project: itself, the frozen
# market-data models, and the pure indicator functions — never risk/execution/
# brokers/ai, which would splice the lab into the live decision path.
_RESEARCH_INTERNAL_ALLOWLIST = (
    "poseidon.research",
    "poseidon.core.models",
    "poseidon.strategy.indicators",
)
_RESEARCH_EXTERNAL_ALLOWLIST = {"structlog"}


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
    """Absolute dotted targets of every import statement, relative imports
    resolved against ``module_name``. ``from X import a`` yields ``X.a`` so a
    submodule import is indistinguishable from a symbol import — prefix checks
    below therefore catch both shapes."""
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


def _research_consumers(module_name: str, source: str, *, is_package: bool = False) -> list[str]:
    """Import targets of a (presumed live) module that land in poseidon.research."""
    return [
        t for t in _import_targets(module_name, source, is_package=is_package)
        if _reaches(t, "poseidon.research")
    ]


def _live_side_imports(module_name: str, source: str, *, is_package: bool = False) -> list[str]:
    """Import targets of a research module that fall outside its allowlist."""
    bad: list[str] = []
    for t in _import_targets(module_name, source, is_package=is_package):
        top = t.split(".")[0]
        if top == "poseidon":
            if not any(_reaches(t, p) for p in _RESEARCH_INTERNAL_ALLOWLIST):
                bad.append(t)
        elif top not in sys.stdlib_module_names and top not in _RESEARCH_EXTERNAL_ALLOWLIST:
            bad.append(t)
    return bad


# ------------------------------------------------- the invariant, on real source


def test_no_live_module_imports_the_research_package() -> None:
    offenders: dict[str, list[str]] = {}
    for name, is_pkg, src in _iter_source_modules():
        if _reaches(name, "poseidon.research") or name in _ALLOWED_CONSUMERS:
            continue
        hits = _research_consumers(name, src, is_package=is_pkg)
        if hits:
            offenders[name] = hits
    assert offenders == {}, (
        f"research/ is offline-only; live modules importing it: {offenders}")


def test_research_imports_no_live_trading_code() -> None:
    checked = 0
    offenders: dict[str, list[str]] = {}
    for name, is_pkg, src in _iter_source_modules():
        if not _reaches(name, "poseidon.research"):
            continue
        checked += 1
        bad = _live_side_imports(name, src, is_package=is_pkg)
        if bad:
            offenders[name] = bad
    # factors, ic, loader, report, __init__ — proves the walk found the package.
    assert checked >= 5
    assert offenders == {}, (
        f"research/ must not import the live side; violations: {offenders}")


# --------------------------------------- the nets bite: synthetic violations


def test_consumer_net_flags_a_live_module_importing_research() -> None:
    for snippet in (
        "from poseidon.research.report import run_report\n",
        "from ..research.factors import ALL_FACTORS\n",
        "from poseidon import research\n",
        "import poseidon.research.loader\n",
        "def f():\n    from ..research import report\n",  # lazy in-function import
    ):
        assert _research_consumers("poseidon.ai.agent", snippet), snippet
    # No false positive on a lookalike package name.
    assert _research_consumers("poseidon.ai.agent", "import poseidon.researcher\n") == []


def test_isolation_net_flags_research_importing_the_live_side() -> None:
    for snippet in (
        "from ..risk.engine import RiskEngine\n",
        "from poseidon.execution.manager import OrderManager\n",
        "from ..brokers.base import Broker\n",
        "from ..ai.agent import ClaudeAgent\n",
        "import poseidon.risk\n",
        "import requests\n",  # third-party beyond structlog: not an offline-lab dep
        "def f():\n    from ..execution import manager\n",  # lazy in-function import
    ):
        assert _live_side_imports("poseidon.research.report", snippet), snippet
    # The legitimate shape stays clean.
    ok = "from ..core.models import Bar\nfrom .factors import Factor\nimport structlog\n"
    assert _live_side_imports("poseidon.research.report", ok) == []
