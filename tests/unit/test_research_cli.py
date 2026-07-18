# tests/unit/test_research_cli.py
"""Parser wiring + config defaults for `poseidon research factors`. The CLI's
network path (loading history, running the report) is NOT unit-tested here —
only that argparse wires the flags to the names cmd_research expects and that
ResearchConfig's defaults match the documented contract."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

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
    # The None/"" sentinels are load-bearing: cmd_research falls back to
    # config.research.* only when a flag was NOT supplied, so an explicit
    # value (validated >= 1 by argparse) is always honored.
    from poseidon.cli import build_parser
    ns = build_parser().parse_args(["research", "factors"])
    assert ns.symbols == "" and ns.symbols_file == "" and ns.watchlist is False
    assert ns.days is None and ns.horizon is None and ns.rebalance_every is None


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


# --- Rigor config additions (design §4.7; spec §7 task 5) --------------------------


def test_research_config_rigor_defaults() -> None:
    # The random-control null / verdict / layering knobs default to the documented
    # policy values; seeds/threshold/groups are config-only (no CLI flag).
    r = AppConfig().research
    assert r.null_seeds == 5 and r.null_base_seed == 42
    assert r.train_frac == 0.0 and r.alpha_t_threshold == 2.0
    assert r.verdict_min_n_eff == 10 and r.n_groups == 5


def test_research_config_rejects_out_of_bounds() -> None:
    # Bad values die at parse (pydantic), never as a surprise mid-run: train_frac must
    # be a unit fraction in [0, 1); at least one null seed; at least two quantile groups.
    with pytest.raises(ValidationError):
        ResearchConfig(train_frac=1.0)
    with pytest.raises(ValidationError):
        ResearchConfig(n_groups=1)
    with pytest.raises(ValidationError):
        ResearchConfig(null_seeds=0)
    with pytest.raises(ValidationError):
        ResearchConfig(verdict_min_n_eff=1)   # ge=2
    with pytest.raises(ValidationError):
        ResearchConfig(alpha_t_threshold=0.0)  # gt=0
    # In-bounds values are accepted.
    ok = ResearchConfig(train_frac=0.5, n_groups=10, null_seeds=1)
    assert ok.train_frac == 0.5 and ok.n_groups == 10 and ok.null_seeds == 1


# --- --train-frac CLI flag (new _unit_fraction argparse type) ----------------------


def test_train_frac_parses_and_defaults_none() -> None:
    from poseidon.cli import build_parser
    assert build_parser().parse_args(["research", "factors"]).train_frac is None
    ns = build_parser().parse_args(["research", "factors", "--train-frac", "0.5"])
    assert ns.train_frac == 0.5


@pytest.mark.parametrize("bad", ["1.0", "-0.1", "1.5"])
def test_train_frac_rejects_non_unit_fraction(bad: str,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    # Must be a float in [0, 1); 1.0 is out (a whole-history "split" has no test segment).
    from poseidon.cli import build_parser
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["research", "factors", "--train-frac", bad])
    assert exc.value.code == 2
    assert "[0, 1)" in capsys.readouterr().err


# --- --universe flag: parse, precedence, resolution --------------------------------


def test_universe_flag_parses_and_rejects_unknown(
        capsys: pytest.CaptureFixture[str]) -> None:
    from poseidon.cli import build_parser
    assert build_parser().parse_args(["research", "factors"]).universe == ""
    ns = build_parser().parse_args(["research", "factors", "--universe", "sp500"])
    assert ns.universe == "sp500"
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["research", "factors", "--universe", "bogus"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_symbols_take_precedence_over_universe() -> None:
    # Precedence: --symbols > --symbols-file > --universe > --watchlist. An explicit
    # --symbols wins and the universe file is never consulted.
    from poseidon.cli import _research_symbols, build_parser
    ns = build_parser().parse_args(
        ["research", "factors", "--symbols", "AAA,BBB", "--universe", "sp500"])
    assert _research_symbols(ns, AppConfig()) == ["AAA", "BBB"]


def test_universe_precedence_over_watchlist(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # --universe outranks --watchlist even when both are supplied.
    from poseidon import cli
    f = tmp_path / "sp500.txt"
    f.write_text("XYZ\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_universe_file", lambda name: f)
    cfg = AppConfig.model_validate(
        {"watchlists": [{"name": "w", "symbols": ["WWW"]}]})
    ns = cli.build_parser().parse_args(
        ["research", "factors", "--universe", "sp500", "--watchlist"])
    assert cli._research_symbols(ns, cfg) == ["XYZ"]


def test_universe_resolves_via_packaged_file(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # --universe reads the packaged data file (resolved through _universe_file), skips
    # #-comment lines, upcases, and order-preservingly dedupes — research/ never reads it.
    from poseidon import cli
    f = tmp_path / "sp500.txt"
    f.write_text("# source: x\n# as_of: 2026-07\nAAA\nbbb\n\n  # note\naaa\nCCC\nBBB\n",
                 encoding="utf-8")
    monkeypatch.setattr(cli, "_universe_file", lambda name: f)
    ns = cli.build_parser().parse_args(["research", "factors", "--universe", "sp500"])
    assert cli._research_symbols(ns, AppConfig()) == ["AAA", "BBB", "CCC"]


def test_symbols_file_skips_comments_and_dedupes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The comment-skip + order-preserving dedupe applies to user --symbols-file too
    # (today a leading '#' line becomes a bogus symbol that fails at load).
    from poseidon.cli import _research_symbols, build_parser
    f = tmp_path / "syms.txt"
    f.write_text("# my list\nAAA\nbbb\n\n  # indented comment\nAAA\nBBB\n", encoding="utf-8")
    ns = build_parser().parse_args(["research", "factors", "--symbols-file", str(f)])
    assert _research_symbols(ns, AppConfig()) == ["AAA", "BBB"]


def test_unit_fraction_type_bounds() -> None:
    # The argparse type directly: [0, 1) — 0 allowed, values >= 1 or < 0 rejected with a
    # usage error. Non-numeric input raises ValueError (argparse maps it to a usage error
    # too, mirroring _positive_int's int() — the CLI-level test above covers that path).
    import argparse

    from poseidon.cli import _unit_fraction
    assert _unit_fraction("0") == 0.0
    assert _unit_fraction("0.999") == 0.999
    for bad in ("1", "1.0", "-0.01"):
        with pytest.raises(argparse.ArgumentTypeError):
            _unit_fraction(bad)
    with pytest.raises(ValueError):
        _unit_fraction("abc")
