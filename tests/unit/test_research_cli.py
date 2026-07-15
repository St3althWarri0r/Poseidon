# tests/unit/test_research_cli.py
"""Parser wiring + config defaults for `poseidon research factors`. The CLI's
network path (loading history, running the report) is NOT unit-tested here —
only that argparse wires the flags to the names cmd_research expects and that
ResearchConfig's defaults match the documented contract."""
from __future__ import annotations

from poseidon.core.config import AppConfig, ResearchConfig


def test_research_config_defaults() -> None:
    r = AppConfig().research
    assert isinstance(r, ResearchConfig)
    assert r.horizon == 5 and r.rebalance_every == 5 and r.min_cross == 5
    assert r.horizons and r.lookback_days >= 100


def test_research_cli_parser_wired() -> None:
    from poseidon.cli import build_parser
    ns = build_parser().parse_args(["research", "factors", "--symbols", "AAA,BBB"])
    assert ns.command == "research" and ns.symbols == "AAA,BBB"


def test_research_cli_parser_defaults() -> None:
    # The 0/"" sentinels are load-bearing: cmd_research falls back to
    # config.research.* with `args.x or config.research.x`.
    from poseidon.cli import build_parser
    ns = build_parser().parse_args(["research", "factors"])
    assert ns.symbols == "" and ns.symbols_file == "" and ns.watchlist is False
    assert ns.days == 0 and ns.horizon == 0 and ns.rebalance_every == 0


def test_research_cli_parser_all_flags() -> None:
    from poseidon.cli import build_parser
    ns = build_parser().parse_args([
        "research", "factors",
        "--symbols-file", "syms.txt",
        "--watchlist",
        "--days", "250",
        "--horizon", "10",
        "--rebalance-every", "3",
    ])
    assert ns.symbols_file == "syms.txt"
    assert ns.watchlist is True
    assert ns.days == 250
    assert ns.horizon == 10
    assert ns.rebalance_every == 3


def test_research_cli_func_dispatches_to_cmd_research() -> None:
    from poseidon.cli import build_parser, cmd_research
    ns = build_parser().parse_args(["research", "factors", "--symbols", "AAA"])
    assert ns.func is cmd_research
