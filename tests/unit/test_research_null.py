# tests/unit/test_research_null.py
"""Within-date random-control null core (design §4.1): the null permutes per-date
scores only — never bars or forward returns — with a string-seeded RNG so every
run is byte-identical. A section 1:1 with an emitted IC sample yields all n_seeds
valid random ICs, so len(alpha_series) == n_periods always; a genuine-drift
universe shows random ICs hover near 0 while alpha stays large."""
from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.factors import Factor
from poseidon.research.ic import (
    _alpha_series,
    _cross_sections,
    _shuffled,
    evaluate_factor,
    spearman,
    union_calendar,
)


def _hist(symbol_series: dict[str, list[float]]) -> dict[str, list[Bar]]:
    hist: dict[str, list[Bar]] = {}
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for sym, closes in symbol_series.items():
        bars = []
        for k, c in enumerate(closes):
            d = base + timedelta(days=k)
            bars.append(Bar(symbol=sym, open=Decimal(str(c)), high=Decimal(str(c)),
                            low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                            start=d, end=d, source="t"))
        hist[sym] = bars
    return hist


def _drift_universe() -> dict[str, list[Bar]]:
    # 8 symbols, each a distinct constant compounding drift => trailing momentum ranks
    # the SAME as the forward return, so per-date IC ~ +1 without any look-ahead.
    series = {}
    for n, drift in enumerate([0.001, 0.003, 0.005, 0.007, 0.009, 0.011, 0.013, 0.015]):
        series[f"S{n}"] = [100 * (1 + drift) ** k for k in range(60)]
    return series


_MOM = Factor("mom5", lambda b: float(b[-1].close) / float(b[-6].close) - 1.0, min_bars=6)


def test_shuffled_preserves_score_multiset_and_does_not_mutate_input() -> None:
    # The permutation preserves the exact score multiset (so spearman on it is defined
    # whenever the base IC was) and never mutates the caller's list — it copies first.
    vals = [0.1, 0.2, 0.2, 0.5, 0.9, 0.9, 1.3, -0.4]
    original = list(vals)
    out = _shuffled(vals, "42:2024-01-05")
    assert sorted(out) == sorted(original)          # same multiset, a genuine permutation
    assert out != original                          # actually shuffled (not the identity)
    assert vals == original                         # input untouched: operates on a copy


def test_alpha_series_permutes_scores_only_and_leaves_forward_returns_untouched() -> None:
    # The null must touch scores only; a section's fwds are the shared-calendar forward
    # returns and must be identical before and after the alpha pass (bars never move).
    hist = _hist(_drift_universe())
    cal = union_calendar(hist)
    dates = cal[::5]
    sections = _cross_sections(_MOM, hist, dates, 5, 5, cal)
    fwds_before = [list(s.fwds) for s in sections]
    ics = [spearman(s.vals, s.fwds) for s in sections]
    _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    assert [list(s.fwds) for s in sections] == fwds_before   # forward returns untouched


def test_alpha_series_is_deterministic_and_base_seed_sensitive() -> None:
    # Same string seed => byte-identical reruns (PYTHONHASHSEED-independent). A different
    # base_seed draws different permutations => a different alpha series.
    hist = _hist(_drift_universe())
    cal = union_calendar(hist)
    dates = cal[::5]
    sections = _cross_sections(_MOM, hist, dates, 5, 5, cal)
    ics = [spearman(s.vals, s.fwds) for s in sections]
    a1 = _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    a2 = _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    a3 = _alpha_series(sections, ics, n_seeds=5, base_seed=99)
    assert a1 == a2                                  # deterministic
    assert a1 != a3                                  # base_seed actually moves the draws


def test_alpha_series_length_equals_n_periods_on_gappy_data() -> None:
    # Mixed history lengths => some rebalance dates fall below min_cross and are skipped,
    # so emitted sections are a strict subset of the grid. Every emitted section still
    # yields all n_seeds valid random ICs (shuffle preserves the multiset), so the alpha
    # series lines up 1:1 with the IC series — never dropped, never padded.
    series = _drift_universe()
    series.update({f"T{n}": [50 * (1 + 0.002 * n) ** k for k in range(28)] for n in range(3)})
    hist = _hist(series)
    cal = union_calendar(hist)
    dates = cal[::5]
    sections = _cross_sections(_MOM, hist, dates, 5, 5, cal)
    ics = [spearman(s.vals, s.fwds) for s in sections]
    alpha = _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    res = evaluate_factor(_MOM, hist, horizon=5, rebalance_every=5, horizons=[5])
    assert 0 < len(sections) < len(dates)           # genuinely gappy: some dates skipped
    assert len(alpha) == len(sections) == res.n_periods


def test_drift_universe_null_random_ic_near_zero_while_alpha_is_large() -> None:
    # On a genuine-signal universe the base IC ~ +1; permuting the scores destroys the
    # signal so the random ICs average near 0, and alpha = IC - mean(random_IC) stays big.
    hist = _hist(_drift_universe())
    cal = union_calendar(hist)
    dates = cal[::5]
    sections = _cross_sections(_MOM, hist, dates, 5, 5, cal)
    ics = [spearman(s.vals, s.fwds) for s in sections]
    assert statistics.fmean(ics) > 0.9              # the raw signal is strong
    random_ics = [spearman(_shuffled(s.vals, f"{42 + k}:{s.t.isoformat()}"), s.fwds)
                  for s in sections for k in range(5)]
    assert abs(statistics.fmean(random_ics)) < 0.3  # permutation control ~ 0
    alpha = _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    assert statistics.fmean(alpha) > 0.5            # survives its own random control
