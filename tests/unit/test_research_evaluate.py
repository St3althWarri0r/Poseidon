# tests/unit/test_research_evaluate.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.core.models import Bar
from poseidon.research.factors import Factor
from poseidon.research.ic import evaluate_factor


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


def test_factor_last_bar_is_never_beyond_its_rebalance_date() -> None:
    # The factor's latest visible bar must always be exactly a rebalance date and
    # never a future bar. If visible_bars leaked a bar dated after t, bars[-1] would
    # be a between/after-rebalance bar not in the rebalance set -> subset fails.
    from poseidon.research.ic import rebalance_dates
    seen: set = set()

    def probe(bars):
        seen.add(bars[-1].end.date())
        return float(len(bars))
    hist = _hist({s: [100 + i for i in range(40)] for s in ("A", "B", "C", "D", "E", "F")})
    rebs = set(rebalance_dates(hist, 5))
    evaluate_factor(Factor("probe", probe, min_bars=2), hist,
                    horizon=1, rebalance_every=5, horizons=[1])
    assert seen and seen <= rebs                    # never a bar dated after its rebalance date


def test_ic_plus_one_non_circular() -> None:
    # 6 symbols; each has a constant per-symbol drift => trailing momentum ranks the
    # SAME as forward return, WITHOUT the factor ever seeing the future.
    series = {}
    for n, drift in enumerate([0.001, 0.003, 0.005, 0.007, 0.009, 0.011]):
        series[f"S{n}"] = [100 * (1 + drift) ** k for k in range(60)]
    hist = _hist(series)
    mom = Factor("mom5", lambda b: b[-1].close.__float__() / float(b[-6].close) - 1.0, min_bars=6)
    res = evaluate_factor(mom, hist, horizon=5, rebalance_every=5, horizons=[5])
    assert res.ic_mean > 0.9                        # harness correlates past-signal with future
    neg = Factor("negmom", lambda b: -(float(b[-1].close) / float(b[-6].close) - 1.0), min_bars=6)
    assert evaluate_factor(neg, hist, horizon=5, rebalance_every=5, horizons=[5]).ic_mean < -0.9


def test_effective_n_formula() -> None:
    # The non-overlap count, tested directly (deterministic, not data-dependent).
    from poseidon.research.ic import _effective_n
    assert _effective_n(11, 20, 5) == 3     # stride ceil(20/5)=4 -> ceil(11/4)=3
    assert _effective_n(10, 5, 5) == 10     # rebalance == horizon -> no overlap
    assert _effective_n(12, 10, 5) == 6     # stride 2
    assert _effective_n(0, 20, 5) == 0


def test_evaluate_factor_rejects_non_positive_horizon_or_rebalance() -> None:
    # A non-positive horizon/rebalance_every/horizons entry would let forward_return's
    # `bars[i + horizon]` negative-index onto a REAL future bar — a silent look-ahead
    # leak. The CLI now accepts a user-supplied --horizon, so this must be a hard error.
    hist = _hist({s: [100 + i for i in range(30)] for s in ("A", "B", "C", "D", "E")})
    probe = Factor("probe", lambda b: float(len(b)), min_bars=2)
    with pytest.raises(ValueError):
        evaluate_factor(probe, hist, horizon=-1, rebalance_every=5, horizons=[1])
    with pytest.raises(ValueError):
        evaluate_factor(probe, hist, horizon=5, rebalance_every=0, horizons=[1])
    with pytest.raises(ValueError):
        evaluate_factor(probe, hist, horizon=5, rebalance_every=5, horizons=[1, 0])


def test_t_stat_uses_non_overlapping_n_eff() -> None:
    # IC must VARY across dates or ic_std=0 -> ir=0 -> t_stat=0 hides the bug. A per-
    # symbol drift ranks the cross-section; a symbol-phased wiggle makes each date's IC
    # high-but-imperfect and different, so ir != 0 and n_eff vs n_periods is testable.
    import math

    from poseidon.research.ic import _effective_n
    series = {f"S{n}": [100 * ((1 + 0.001 * n) ** i) * (1 + 0.02 * math.sin(0.3 * i + n))
                        for i in range(140)] for n in range(8)}
    hist = _hist(series)
    mom = Factor("m", lambda b: float(b[-1].close) / float(b[-11].close) - 1.0, min_bars=11)
    res = evaluate_factor(mom, hist, horizon=20, rebalance_every=5, horizons=[20])
    assert res.n_periods >= 2 and abs(res.ir) > 1e-9     # data produced a genuinely varying IC
    n_eff = _effective_n(res.n_periods, 20, 5)
    assert n_eff < res.n_periods                         # overlap present
    assert abs(res.t_stat - res.ir * math.sqrt(n_eff)) < 1e-9           # uses n_eff
    assert abs(res.t_stat - res.ir * math.sqrt(res.n_periods)) > 1e-9   # NOT the raw period count


def _wiggle_universe(length: int, n_syms: int = 8) -> dict[str, list[Bar]]:
    # Per-symbol drift ranks the cross-section; a symbol-phased wiggle makes each date's
    # IC high-but-imperfect and genuinely varying, so alpha/t-stats are non-degenerate.
    import math
    return _hist({f"S{n}": [100 * ((1 + 0.002 * n) ** i) * (1 + 0.02 * math.sin(0.3 * i + n))
                            for i in range(length)] for n in range(n_syms)})


def test_alpha_t_uses_non_overlapping_n_eff() -> None:
    # The paired alpha t-stat must use the SAME non-overlapping n_eff as the base t-stat
    # (design §4.2 — the alpha pairs inherit the base series' overlap), never the raw
    # period count. Mirrors test_t_stat_uses_non_overlapping_n_eff for the alpha series.
    from poseidon.research.ic import (
        _alpha_series,
        _cross_sections,
        _effective_n,
        _t_stat,
        spearman,
        union_calendar,
    )
    hist = _wiggle_universe(140)
    mom = Factor("m", lambda b: float(b[-1].close) / float(b[-11].close) - 1.0, min_bars=11)
    res = evaluate_factor(mom, hist, horizon=20, rebalance_every=5, horizons=[20])
    # Rebuild the exact alpha series the evaluator used (default NullSpec: 5 seeds, base 42).
    cal = union_calendar(hist)
    dates = cal[::5]
    sections = _cross_sections(mom, hist, dates, 20, 5, cal)
    ics = [spearman(s.vals, s.fwds) for s in sections]
    alpha = _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    n_eff = _effective_n(res.n_periods, 20, 5)
    assert res.n_eff == n_eff and n_eff < res.n_periods         # overlap present, stored on result
    assert res.alpha_t is not None and abs(res.alpha_t) > 1e-9  # a genuinely varying alpha series
    assert abs(res.alpha_t - _t_stat(alpha, n_eff)) < 1e-9              # paired t uses n_eff
    assert abs(res.alpha_t - _t_stat(alpha, res.n_periods)) > 1e-9      # NOT the raw period count


def test_split_at_floor_n_times_frac() -> None:
    # A chronological OOS split (train_frac>0) cuts the emitted alpha series at
    # k = floor(n * train_frac); with rebalance == horizon the embargo (stride-1) is 0,
    # so train = alpha[:k] and test = alpha[k:], each scored by its own _t_stat/_effective_n.
    import math

    from poseidon.research.ic import (
        NullSpec,
        _alpha_series,
        _cross_sections,
        _effective_n,
        _t_stat,
        spearman,
        union_calendar,
    )
    hist = _wiggle_universe(200)
    mom = Factor("m", lambda b: float(b[-1].close) / float(b[-6].close) - 1.0, min_bars=6)
    res = evaluate_factor(mom, hist, horizon=5, rebalance_every=5, horizons=[5],
                          null=NullSpec(train_frac=0.5))
    cal = union_calendar(hist)
    dates = cal[::5]
    sections = _cross_sections(mom, hist, dates, 5, 5, cal)
    ics = [spearman(s.vals, s.fwds) for s in sections]
    alpha = _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    n = len(alpha)
    k = math.floor(n * 0.5)
    train, test = alpha[:k], alpha[k:]                          # stride 1 -> no embargo
    assert res.alpha_t_train is not None and res.alpha_t_test is not None
    assert abs(res.alpha_t_train - _t_stat(train, _effective_n(len(train), 5, 5))) < 1e-9
    assert abs(res.alpha_t_test - _t_stat(test, _effective_n(len(test), 5, 5))) < 1e-9


def test_embargo_drops_stride_minus_one_test_samples() -> None:
    # On an overlapping grid (horizon=20, rebalance=5 -> stride 4) the test segment drops
    # its first stride-1 = 3 samples so no test window overlaps a train window. The stored
    # alpha_t_test must match the embargoed segment, not the naive alpha[k:].
    import math

    from poseidon.research.ic import (
        NullSpec,
        _alpha_series,
        _cross_sections,
        _effective_n,
        _t_stat,
        spearman,
        union_calendar,
    )
    hist = _wiggle_universe(200)
    mom = Factor("m", lambda b: float(b[-1].close) / float(b[-11].close) - 1.0, min_bars=11)
    res = evaluate_factor(mom, hist, horizon=20, rebalance_every=5, horizons=[20],
                          null=NullSpec(train_frac=0.5))
    cal = union_calendar(hist)
    dates = cal[::5]
    sections = _cross_sections(mom, hist, dates, 20, 5, cal)
    ics = [spearman(s.vals, s.fwds) for s in sections]
    alpha = _alpha_series(sections, ics, n_seeds=5, base_seed=42)
    n = len(alpha)
    k = math.floor(n * 0.5)
    stride = math.ceil(20 / 5)
    assert stride == 4
    embargoed, naive = alpha[k + stride - 1:], alpha[k:]
    assert len(embargoed) == len(naive) - (stride - 1)          # 3 test samples dropped
    assert res.alpha_t_test is not None
    assert abs(res.alpha_t_test - _t_stat(embargoed, _effective_n(len(embargoed), 20, 5))) < 1e-9
    assert abs(res.alpha_t_test - _t_stat(naive, _effective_n(len(naive), 20, 5))) > 1e-9


def test_tiny_split_segment_yields_none_train_test() -> None:
    # When a split segment falls below _MIN_SPLIT_SAMPLES after the embargo the split is
    # n/a: both alpha_t_train and alpha_t_test are None (never a silent 0.0), while the
    # full-sample alpha_t is still computed so the verdict can fall back to it.
    from poseidon.research.ic import NullSpec
    hist = _hist({f"S{n}": [100 * (1 + 0.003 * n) ** k for k in range(60)] for n in range(8)})
    mom = Factor("mom5", lambda b: float(b[-1].close) / float(b[-6].close) - 1.0, min_bars=6)
    res = evaluate_factor(mom, hist, horizon=5, rebalance_every=5, horizons=[5],
                          null=NullSpec(train_frac=0.9))
    assert res.n_periods > 0 and res.alpha_t is not None        # full-sample alpha still computed
    assert res.alpha_t_train is None and res.alpha_t_test is None   # segment too small -> split n/a


def test_evaluate_factor_rejects_bad_nullspec() -> None:
    # NullSpec is an unvalidated frozen dataclass, so direct callers can pass nonsense;
    # evaluate_factor guards defensively (same style as the horizon guard, design §4.7/§5).
    from poseidon.research.ic import NullSpec
    hist = _hist({s: [100 + i for i in range(30)] for s in ("A", "B", "C", "D", "E")})
    probe = Factor("probe", lambda b: float(len(b)), min_bars=2)
    for bad in (NullSpec(n_seeds=0), NullSpec(train_frac=1.0), NullSpec(train_frac=-0.1),
                NullSpec(alpha_t_threshold=0.0), NullSpec(min_n_eff=1)):
        with pytest.raises(ValueError):
            evaluate_factor(probe, hist, horizon=5, rebalance_every=5, horizons=[5], null=bad)


# --- Verdict function (design §4.3; spec §7 task 3) ---------------------------------
# Pure, table-driven tests over _verdict. Default NullSpec: alpha_t_threshold=2.0,
# min_n_eff=10. A "passing" baseline clears every gate leg AND the null so the tables
# below isolate exactly the leg each case perturbs.


def _passing() -> dict:
    # Every gate leg passes AND alpha survives the null: ic_mean>0.02, hit>=0.55,
    # |t|>2, alpha_t>=2.0; n_eff above the floor; no split by default.
    return {"n_eff": 20, "ic_mean": 0.05, "hit_rate": 0.60, "t_stat": 3.0, "alpha_t": 3.0,
            "alpha_t_test": None, "split_ran": False}


def test_verdict_insufficient_data_below_n_eff_floor() -> None:
    # Rule 1: n_eff < null.min_n_eff (default 10) -> insufficient_data, regardless of how
    # strong the other stats look (few dates never yield a confident category). n_eff==0
    # (n_periods==0) is the same branch.
    from poseidon.research.ic import NullSpec, _verdict
    null = NullSpec()
    for n_eff in (0, 1, 9):
        assert _verdict(**{**_passing(), "n_eff": n_eff}, null=null) == "insufficient_data"
    assert _verdict(**{**_passing(), "n_eff": 10}, null=null) != "insufficient_data"  # floor is exclusive-below


def test_verdict_reversed_full_sample() -> None:
    # Rule 2: alpha_t <= -threshold -> reversed (significantly worse than its own random
    # control). Fires BEFORE the gate, even when ic_mean/hit/t would otherwise pass.
    from poseidon.research.ic import NullSpec, _verdict
    null = NullSpec()
    assert _verdict(**{**_passing(), "alpha_t": -2.0}, null=null) == "reversed"   # exactly -thr
    assert _verdict(**{**_passing(), "alpha_t": -5.0}, null=null) == "reversed"
    # Just inside the reversed band (not <= -thr) and failing the gate -> noise, not reversed.
    assert _verdict(**{**_passing(), "alpha_t": -1.9}, null=null) == "noise"


def test_verdict_noise_each_gate_leg_failing_alone() -> None:
    # Rule 3: the gate is ic_mean>0.02 AND hit>=0.55 AND |t|>2 AND alpha_t>=thr. With the
    # other three legs passing, any single failing leg -> noise. The alpha_t leg failing
    # while raw IC/t pass is the VT 12/12 -> 1/12 case (strong raw signal, no null survival).
    from poseidon.research.ic import NullSpec, _verdict
    null = NullSpec()
    cases = {
        "ic_mean": {"ic_mean": 0.02},          # not strictly > 0.02
        "hit_rate": {"hit_rate": 0.54},        # < 0.55
        "t_stat": {"t_stat": 2.0},             # |t| not > 2
        "alpha_t": {"alpha_t": 1.9},           # 0 < alpha_t < thr: raw IC passes, null fails (VT)
    }
    for leg, override in cases.items():
        assert _verdict(**{**_passing(), **override}, null=null) == "noise", leg
    # Negative t magnitude still clears |t|>2, so a strong-but-negative t alone stays a gate
    # pass on that leg (ic_mean leg then decides): confirm |t| uses magnitude.
    assert _verdict(**{**_passing(), "t_stat": -3.0}, null=null) == "confirmed_alive"


def test_verdict_confirmed_alive_full_sample_and_oos_survival() -> None:
    # Rule 5: gate passed, no split -> confirmed_alive. Rule 4: gate passed, split ran and
    # alpha_t_test >= thr -> confirmed_alive (OOS survival).
    from poseidon.research.ic import NullSpec, _verdict
    null = NullSpec()
    assert _verdict(**_passing(), null=null) == "confirmed_alive"                 # no split
    assert _verdict(**{**_passing(), "split_ran": True, "alpha_t_test": 2.0},
                    null=null) == "confirmed_alive"                               # OOS survives (exactly thr)


def test_verdict_train_only_when_oos_neither_survives_nor_reverses() -> None:
    # Rule 4: gate passed, split ran, -thr < alpha_t_test < thr -> train_only (holds in
    # sample, fades out of sample without flipping sign).
    from poseidon.research.ic import NullSpec, _verdict
    null = NullSpec()
    for oos in (1.9, 0.0, -1.9):
        assert _verdict(**{**_passing(), "split_ran": True, "alpha_t_test": oos},
                        null=null) == "train_only", oos


def test_verdict_reversed_on_oos_sign_flip() -> None:
    # Rule 4: gate passed, split ran, alpha_t_test <= -thr -> reversed (out-of-sample sign
    # flip), distinct from a full-sample reversal.
    from poseidon.research.ic import NullSpec, _verdict
    null = NullSpec()
    assert _verdict(**{**_passing(), "split_ran": True, "alpha_t_test": -2.0},
                    null=null) == "reversed"
    assert _verdict(**{**_passing(), "split_ran": True, "alpha_t_test": -4.0},
                    null=null) == "reversed"


def test_verdict_split_na_falls_back_to_full_sample() -> None:
    # When the split is n/a (split_ran False, alpha_t_test None) the verdict falls back to
    # the full-sample logic (rule 5) — never a silent crash on the missing OOS value.
    from poseidon.research.ic import NullSpec, _verdict
    null = NullSpec()
    assert _verdict(**{**_passing(), "split_ran": False, "alpha_t_test": None},
                    null=null) == "confirmed_alive"
    # Full-sample gate fails while split n/a -> noise (fallback, not an OOS category).
    assert _verdict(**{**_passing(), "alpha_t": 1.0, "split_ran": False, "alpha_t_test": None},
                    null=null) == "noise"


def test_verdict_respects_configurable_alpha_t_threshold() -> None:
    # alpha_t_threshold is configurable (HLZ: 3.5 for whole-library scans). An alpha_t of
    # 3.0 that confirms at thr=2.0 becomes gate-failing noise at thr=3.5.
    from poseidon.research.ic import NullSpec, _verdict
    assert _verdict(**{**_passing(), "alpha_t": 3.0}, null=NullSpec()) == "confirmed_alive"
    assert _verdict(**{**_passing(), "alpha_t": 3.0},
                    null=NullSpec(alpha_t_threshold=3.5)) == "noise"


def test_evaluate_factor_stores_verdict_on_result() -> None:
    # Wiring: evaluate_factor computes _verdict and stores it on ICResult.verdict. A drift+
    # wiggle universe has a high-but-varying IC (so t_stat is defined and large) that
    # survives its own random control -> confirmed_alive (no split by default). A perfectly
    # clean drift instead has ic_std == 0 -> t_stat 0 -> the |t|>2 gate leg fails -> noise;
    # both are asserted so the verdict is genuinely wired, not a constant.
    from poseidon.research.ic import NullSpec
    mom = Factor("mom5", lambda b: float(b[-1].close) / float(b[-6].close) - 1.0, min_bars=6)
    wiggle = _wiggle_universe(140)
    res = evaluate_factor(mom, wiggle, horizon=5, rebalance_every=5, horizons=[5],
                          null=NullSpec(min_n_eff=2))
    assert res.n_eff >= 2 and res.verdict == "confirmed_alive"
    clean = _hist({f"S{n}": [100 * (1 + 0.003 * n) ** k for k in range(120)] for n in range(8)})
    res_clean = evaluate_factor(mom, clean, horizon=5, rebalance_every=5, horizons=[5],
                                null=NullSpec(min_n_eff=2))
    assert res_clean.ic_std == 0.0 and res_clean.verdict == "noise"   # zero-variance IC -> |t| fails
    # No-data factor (impossible min_bars) -> n_periods 0 -> insufficient_data default holds.
    dead = Factor("dead", lambda b: float(b[-1].close), min_bars=10_000)
    res0 = evaluate_factor(dead, wiggle, horizon=5, rebalance_every=5, horizons=[5])
    assert res0.n_periods == 0 and res0.verdict == "insufficient_data"
