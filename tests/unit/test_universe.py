"""TASK 1: bundled screener universe file + pure loader (severed from research/).

The live market screener must NOT read into ``research/`` (safety invariant 4),
so it ships its OWN copy of the S&P 500 constituent list under ``data/universe/``
and reads it through a pure loader that imports only stdlib + ``core.errors``.
A drift guard asserts the live copy stays byte-identical to the research copy so
the two snapshots never diverge silently.
"""
from __future__ import annotations

import ast
from importlib.resources import files
from pathlib import Path

import pytest

from poseidon.core.errors import ConfigError
from poseidon.data.universe import load_universe


def test_load_sp500_501_unique_upper() -> None:
    symbols = load_universe("sp500")
    assert len(symbols) == 501
    assert len(set(symbols)) == len(symbols)  # de-duped
    assert all(s == s.upper() for s in symbols)  # uppercased
    assert symbols[0] == "MMM"  # order-stable: first ticker in the file


def test_skips_comments_and_blanks() -> None:
    symbols = load_universe("sp500")
    assert not any(s.startswith("#") for s in symbols)  # header comment dropped
    assert all(s and s.strip() == s for s in symbols)  # no blank / untrimmed rows


def test_unknown_universe_raises() -> None:
    with pytest.raises(ConfigError):
        load_universe("does_not_exist")


def test_screener_universe_matches_research_copy() -> None:
    """Drift guard: the live screener copy must equal the research copy byte-for-byte."""
    live = (files("poseidon.data") / "universe" / "sp500.txt").read_text(encoding="utf-8")
    research = (files("poseidon.research") / "data" / "sp500.txt").read_text(encoding="utf-8")
    assert live == research


def test_load_universe_has_no_research_import() -> None:
    """The loader module must not import ``poseidon.research`` in any import shape."""
    import poseidon.data.universe as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert all("research" not in alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or "research" not in node.module
