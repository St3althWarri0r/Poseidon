# tests/unit/test_research_report.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.factors import ALL_FACTORS, Factor
from poseidon.research.ic import NullSpec
from poseidon.research.report import run_report

_VERDICTS = {"insufficient_data", "reversed", "noise", "confirmed_alive", "train_only"}


def _hist(n_syms: int, n_days: int) -> dict[str, list[Bar]]:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    hist = {}
    for s in range(n_syms):
        bars = []
        for k in range(n_days):
            c = 100 + s + k * 0.1
            d = base + timedelta(days=k)
            bars.append(Bar(symbol=f"S{s}", open=Decimal(str(c)), high=Decimal(str(c)),
                            low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                            start=d, end=d, source="t"))
        hist[f"S{s}"] = bars
    return hist


def test_report_ranks_and_renders() -> None:
    rep = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5, horizons=[1, 5, 10])
    assert len(rep.results) == len(ALL_FACTORS)
    ts = [abs(r.t_stat) for r in rep.results]
    assert ts == sorted(ts, reverse=True)          # sorted by |t_stat| desc
    assert "IC" in rep.render() and "factor" in rep.render().lower()


def test_report_flags_thin_universe() -> None:
    rep = run_report(ALL_FACTORS, _hist(3, 300), horizon=5, rebalance_every=5, horizons=[5])
    assert rep.thin is True                          # 3 symbols is too thin to trust
    assert "thin" in rep.render().lower() or "noisy" in rep.render().lower()


# --- Rigor report render (design §4.8; spec §7 task 6) -----------------------------


def test_report_renders_alpha_columns_and_verdict() -> None:
    # The main table gains alphaIC / alpha_t / verdict columns before the decay cell;
    # every evaluated row carries one of the honest verdict categories.
    rep = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5,
                     horizons=[1, 5, 10])
    out = rep.render()
    assert "alphaIC" in out and "alpha_t" in out and "verdict" in out
    for r in rep.results:
        if r.n_periods == 0:
            continue
        assert r.verdict in _VERDICTS
        row = next(line for line in out.splitlines() if line.startswith(r.factor))
        assert r.verdict in row                       # the row renders its own verdict


def test_report_no_data_row_unchanged_with_new_columns() -> None:
    # A never-evaluated factor still renders the "no data" row, never a fake +0.0000
    # alpha column — the finding-14 discipline extends to the new fields.
    hist = _hist(6, 40)
    needy = Factor("needs_500", lambda b: float(len(b)), min_bars=500)
    rep = run_report([needy], hist, horizon=5, rebalance_every=5, horizons=[1, 5])
    row = next(line for line in rep.render().splitlines() if line.startswith("needs_500"))
    assert "no data" in row
    assert "+0.0000" not in row


def test_report_hlz_note_always_present() -> None:
    # The Harvey-Liu-Zhu multiple-testing caveat is a permanent footer note, printed
    # regardless of universe or split.
    rep = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5, horizons=[5])
    out = rep.render()
    assert "Harvey-Liu-Zhu" in out and "3.5" in out
    assert "alpha_IC" in out                          # the alpha_IC definition note


def test_report_survivorship_caveat_only_with_universe_note() -> None:
    plain = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5,
                       horizons=[5]).render()
    assert "UNIVERSE CAVEAT" not in plain and "survivorship" not in plain.lower()

    labeled = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5,
                         horizons=[5], universe_note="sp500").render()
    assert "UNIVERSE CAVEAT [sp500]" in labeled
    assert "survivorship" in labeled.lower()


def test_report_oos_split_block_only_when_split_runs() -> None:
    off = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5,
                     horizons=[5], null=NullSpec(train_frac=0.0)).render()
    assert "OOS split" not in off

    on = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5,
                    horizons=[5], null=NullSpec(train_frac=0.5)).render()
    assert "OOS split" in on
    assert "train_frac=0.50" in on and "embargo=0 samples" in on


def test_report_quantile_section_insufficient_path() -> None:
    # 8-name universe: breadth 8 < 3*5 -> every factor's layering is insufficient and
    # renders the reason instead of a confident L/S readout.
    rep = run_report(ALL_FACTORS, _hist(8, 300), horizon=5, rebalance_every=5, horizons=[5])
    out = rep.render()
    assert "Quantile layering (n_groups=5" in out
    assert "insufficient data (breadth 8 < 15)" in out


def test_report_quantile_section_confident_readout() -> None:
    # A broad universe clears the layering floors, so the section prints a real
    # long/short readout rather than the insufficient reason.
    rep = run_report(ALL_FACTORS, _hist(20, 300), horizon=5, rebalance_every=5, horizons=[5])
    out = rep.render()
    assert "Quantile layering (n_groups=5" in out
    assert "L/S" in out and "mono rho" in out


def test_sp500_universe_file_parses_to_broad_snapshot() -> None:
    # The bundled snapshot has # header comments (source / as_of / survivorship) and
    # parses to a broad, de-duplicated symbol list keeping dot-class tickers.
    from poseidon.cli import _symbols_from_lines, _universe_file

    raw = _universe_file("sp500").read_text(encoding="utf-8")
    lines = raw.splitlines()
    header = [ln for ln in lines if ln.strip().startswith("#")]
    assert any("source" in ln.lower() for ln in header)
    assert any("as_of" in ln.lower() for ln in header)
    assert any("survivorship" in ln.lower() for ln in header)

    symbols = _symbols_from_lines(lines)
    assert len(symbols) >= 400
    assert len(set(symbols)) == len(symbols)          # already de-duplicated
    assert "BRK.B" in symbols                          # dot-class tickers kept
