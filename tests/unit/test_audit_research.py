# tests/unit/test_audit_research.py
"""Regression tests for the research-math audit findings: shared-calendar forward
windows in mixed-calendar universes (13), honest no-data rendering (14), thin flag
keyed on effective cross-section (15), and CLI validation of --days/--horizon/
--rebalance-every (16)."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.core.models import Bar
from poseidon.research.factors import Factor
from poseidon.research.ic import evaluate_factor
from poseidon.research.report import run_report

_FRIDAY = datetime(2024, 1, 5, tzinfo=UTC)          # 2024-01-05 is a Friday


def _bar(sym: str, day: int, close: float) -> Bar:
    d = _FRIDAY + timedelta(days=day)
    c = Decimal(str(close))
    return Bar(symbol=sym, open=c, high=c, low=c, close=c, volume=100,
               start=d, end=d, source="t")


def _daily(sym: str, closes: list[float]) -> list[Bar]:
    """One bar per calendar day (crypto-style 7d/wk calendar)."""
    return [_bar(sym, k, c) for k, c in enumerate(closes)]


def _weekday_series(sym: str, weekly_closes: list[float], weeks: int) -> list[Bar]:
    """`weekly_closes` = [Fri, Mon, Tue, Wed, Thu] repeated for `weeks` weeks,
    skipping weekends (equity-style 5d/wk calendar). Day 0 is a Friday."""
    offsets = [0, 3, 4, 5, 6]                        # Fri, Mon, Tue, Wed, Thu
    bars = []
    for w in range(weeks):
        for off, c in zip(offsets, weekly_closes, strict=True):
            bars.append(_bar(sym, 7 * w + off, c))
    return bars


# ---------------------------------------------------------------- finding 13


def test_mixed_calendar_forward_windows_share_the_rebalance_calendar() -> None:
    # 4 crypto-style symbols trade 7d/wk; one equity trades 5d/wk. Rebalancing every
    # 7 union dates puts every sample on a Friday; horizon=3 must mean "3 shared
    # calendar dates" (Friday -> Monday close) for EVERY symbol. The equity is built
    # to rally into Monday (+10%) then crash by Wednesday (-10%): if its horizon is
    # measured in its OWN bar space (3 equity bars = Fri -> Wed), its forward return
    # flips sign and the cross-sectional IC collapses from +1 to 0.
    hist: dict[str, list[Bar]] = {}
    for n in range(4):
        g = 0.003 * (n + 1)                          # Fri->Mon return (1+g)^3-1 < 10%
        hist[f"C{n}"] = _daily(f"C{n}", [100 * (1 + g) ** k for k in range(35)])
    hist["EQ"] = _weekday_series("EQ", [100.0, 110.0, 90.0, 90.0, 100.0], weeks=5)

    scores = {"C0": 1.0, "C1": 2.0, "C2": 3.0, "C3": 4.0, "EQ": 5.0}
    probe = Factor("probe", lambda bars: scores[bars[0].symbol], min_bars=2)
    res = evaluate_factor(probe, hist, horizon=3, rebalance_every=7, horizons=[3])

    assert res.n_periods >= 3                        # Fridays with a full forward window
    assert res.ic_mean > 0.99                        # perfect rank agreement on the shared calendar


# ---------------------------------------------------------------- finding 14


def _flat_universe(n_syms: int, n_days: int) -> dict[str, list[Bar]]:
    return {f"S{s}": _daily(f"S{s}", [100.0 + s + 0.1 * k for k in range(n_days)])
            for s in range(n_syms)}


def test_no_data_factor_renders_distinctly_from_zero_ic() -> None:
    # A factor whose min_bars exceeds the history is never evaluated; it must not
    # render as IC +0.0000 (indistinguishable from measured-zero alpha).
    hist = _flat_universe(6, 40)
    needy = Factor("needs_200", lambda b: float(len(b)), min_bars=200)
    ok = Factor("len_probe", lambda b: float(b[-1].close), min_bars=2)
    rep = run_report([needy, ok], hist, horizon=5, rebalance_every=5, horizons=[1, 5])

    needy_res = next(r for r in rep.results if r.factor == "needs_200")
    ok_res = next(r for r in rep.results if r.factor == "len_probe")
    assert needy_res.n_periods == 0 and ok_res.n_periods > 0

    needy_row = next(line for line in rep.render().splitlines()
                     if line.startswith("needs_200"))
    assert "no data" in needy_row                    # rendered as never-evaluated
    assert "+0.0000" not in needy_row                # not as a genuinely-zero IC
    # decay horizons with no samples are None, not a fake 0.0
    assert needy_res.ic_by_horizon == {1: None, 5: None}


def test_report_surfaces_n_periods() -> None:
    hist = _flat_universe(6, 40)
    ok = Factor("len_probe", lambda b: float(b[-1].close), min_bars=2)
    rep = run_report([ok], hist, horizon=5, rebalance_every=5, horizons=[5])
    row = next(line for line in rep.render().splitlines() if line.startswith("len_probe"))
    assert f" {rep.results[0].n_periods} " in f"{row} "


# ---------------------------------------------------------------- finding 15


def test_thin_flag_keys_on_effective_cross_section() -> None:
    # 25 symbols have SOME bars, but only 5 have enough history for the factor, so
    # every IC sample is a 5-name Spearman — the report must flag THIN even though
    # the nominal universe is >= 20 names.
    hist = _flat_universe(5, 300)
    hist.update({f"T{s}": _daily(f"T{s}", [50.0 + s + 0.1 * k for k in range(30)])
                 for s in range(20)})
    deep = Factor("deep", lambda b: float(b[-1].close), min_bars=250)
    rep = run_report([deep], hist, horizon=5, rebalance_every=5, horizons=[5])

    assert rep.cross_section_size == 25
    assert rep.results[0].n_periods > 0              # the factor DID evaluate
    assert rep.results[0].breadth == 5               # ...on 5-name cross-sections
    assert rep.thin is True


def test_broad_effective_cross_section_is_not_thin() -> None:
    hist = _flat_universe(25, 100)
    shallow = Factor("shallow", lambda b: float(b[-1].close), min_bars=2)
    rep = run_report([shallow], hist, horizon=5, rebalance_every=5, horizons=[5])
    assert rep.results[0].breadth == 25
    assert rep.thin is False


# ---------------------------------------------------------------- finding 16


def _parse(argv: list[str]) -> argparse.Namespace:
    from poseidon.cli import build_parser
    return build_parser().parse_args(argv)


@pytest.mark.parametrize("flag", ["--days", "--horizon", "--rebalance-every"])
def test_cli_rejects_negative_research_ints(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(["research", "factors", "--symbols", "AAA,BBB", flag, "-5"])
    assert exc.value.code == 2                       # argparse usage error, not a traceback
    assert "must be an integer >= 1" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--days", "--horizon", "--rebalance-every"])
def test_cli_rejects_explicit_zero_instead_of_silently_defaulting(
        flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    # An explicit 0 must be an error — silently substituting the config default runs
    # a different experiment than the user asked for.
    with pytest.raises(SystemExit) as exc:
        _parse(["research", "factors", "--symbols", "AAA,BBB", flag, "0"])
    assert exc.value.code == 2
    assert "must be an integer >= 1" in capsys.readouterr().err


def test_cli_research_int_defaults_are_none_so_config_fills_in() -> None:
    ns = _parse(["research", "factors"])
    assert ns.days is None and ns.horizon is None and ns.rebalance_every is None
