"""Point-in-time IC/IR benchmarking. Pure — no I/O. A factor is handed only the
sliced past window, so look-ahead leakage is impossible at the factor boundary.

Mixed-calendar semantics: the rebalance grid AND every forward-return horizon are
measured on the shared union calendar of the universe's trading dates. In a mixed
universe (e.g. 7d/wk crypto alongside 5d/wk equities) this keeps every symbol's
forward window spanning the same calendar interval, and it makes the non-overlap
stride in `_effective_n` exact, because horizon and rebalance_every are counted in
the same calendar units for all symbols. A symbol that did not trade inside a
window (or whose history ends before the window closes) contributes no sample."""
from __future__ import annotations

import math
import random
import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from ..core.models import Bar
from .factors import Factor


def visible_bars(bars: list[Bar], as_of: date) -> list[Bar]:
    """Bars with end.date() <= as_of (bars are chronological ascending)."""
    return [b for b in bars if b.end.date() <= as_of]


def forward_return(bars: list[Bar], as_of: date, horizon: int,
                   calendar: list[date]) -> float | None:
    """Return from the last close visible at `as_of` to the last close visible at the
    date `horizon` steps after `as_of` on the shared `calendar` (sorted ascending).
    Measuring the horizon on the shared calendar — not per-symbol bar counts — keeps
    forward windows calendar-comparable across symbols with different trading weeks.
    None when the window extends past the calendar, the symbol's history ends before
    the window closes, or the symbol did not trade inside the window."""
    vis = visible_bars(bars, as_of)
    if not vis:
        return None
    k = bisect_right(calendar, as_of)                 # calendar[k-1] is the grid date <= as_of
    j = k - 1 + horizon
    if k == 0 or j >= len(calendar):
        return None
    t_fwd = calendar[j]
    if bars[-1].end.date() < t_fwd:                   # history ends inside the window
        return None
    i = len(vis) - 1
    fut = bisect_right(bars, t_fwd, key=lambda b: b.end.date()) - 1
    if fut <= i:                                      # no bar inside (as_of, t_fwd]
        return None
    base = float(bars[i].close)
    if base == 0.0:
        return None
    return float(bars[fut].close) / base - 1.0


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda k: xs[k])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0                 # average rank for ties (1-based)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation, or None for n<3 or a constant vector."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    try:
        return statistics.correlation(_ranks(xs), _ranks(ys))
    except statistics.StatisticsError:            # constant input -> undefined
        return None


def union_calendar(history: dict[str, list[Bar]]) -> list[date]:
    """Sorted union of every symbol's trading dates — the shared calendar on which
    both the rebalance grid and forward-return horizons are measured."""
    return sorted({b.end.date() for bars in history.values() for b in bars})


def rebalance_dates(history: dict[str, list[Bar]], every: int) -> list[date]:
    """Every `every`-th date of the shared union calendar."""
    return union_calendar(history)[::max(1, every)]


@dataclass(frozen=True)
class ICResult:
    """When n_periods == 0 the factor was never evaluated (insufficient history for
    min_bars/horizon) and the summary stats are 0.0 placeholders, not measured
    zeros; ic_by_horizon holds None for horizons with no samples. `breadth` is the
    median per-date cross-section size over emitted samples (0 when none)."""

    factor: str
    horizon: int
    ic_mean: float
    ic_std: float
    ir: float
    t_stat: float
    hit_rate: float
    n_periods: int
    ic_by_horizon: dict[int, float | None]
    breadth: int
    # Appended, keyword-constructed null/split fields (design §4.6, finding-14 discipline):
    # alpha_mean/alpha_t are None IFF n_periods == 0 (never-evaluated != measured 0.0);
    # alpha_t_train/alpha_t_test are None when the chronological split is off or n/a.
    alpha_mean: float | None = None
    alpha_t: float | None = None
    alpha_t_train: float | None = None
    alpha_t_test: float | None = None
    n_eff: int = 0
    # Honest verdict from the loose base gate AND the random-control null (design §4.3).
    # Defaults to "insufficient_data" so a never-evaluated factor (n_periods == 0) reads as
    # unmeasured, not as a measured category.
    verdict: str = "insufficient_data"


@dataclass(frozen=True)
class _CrossSection:
    """One rebalance date's point-in-time cross-section: factor scores index-aligned
    with shared-calendar forward returns. Only dates whose spearman(vals, fwds) is
    defined become a section, so the section list is 1:1 with the emitted IC series."""

    t: date
    vals: list[float]        # factor scores, index-aligned with fwds
    fwds: list[float]        # shared-calendar forward returns


def _cross_sections(factor: Factor, history: dict[str, list[Bar]], dates: list[date],
                    horizon: int, min_cross: int,
                    calendar: list[date]) -> list[_CrossSection]:
    """Per-date cross-sections under exactly the base IC gating (min_bars, None score/
    forward, >= min_cross names, spearman defined). A section is kept only when its
    spearman IC is not None, so `_cross_sections` output is 1:1 with the emitted IC
    samples — the base IC and the random-control null share this single pass, and the
    factor fn is called once per (date, symbol)."""
    sections: list[_CrossSection] = []
    for t in dates:
        vals: list[float] = []
        fwds: list[float] = []
        for bars in history.values():
            vis = visible_bars(bars, t)
            if len(vis) < factor.min_bars:
                continue
            v = factor.fn(vis)
            if v is None:
                continue
            r = forward_return(bars, t, horizon, calendar)
            if r is None:
                continue
            vals.append(v)
            fwds.append(r)
        if len(vals) >= min_cross and spearman(vals, fwds) is not None:
            sections.append(_CrossSection(t, vals, fwds))
    return sections


def _ic_series(factor: Factor, history: dict[str, list[Bar]], dates: list[date],
               horizon: int, min_cross: int,
               calendar: list[date]) -> tuple[list[float], list[int]]:
    """IC per rebalance date plus each emitted sample's cross-section size, derived
    from the shared per-date cross-section pass."""
    series: list[float] = []
    widths: list[int] = []
    for s in _cross_sections(factor, history, dates, horizon, min_cross, calendar):
        ic = spearman(s.vals, s.fwds)
        assert ic is not None                     # _cross_sections keeps only defined ICs
        series.append(ic)
        widths.append(len(s.vals))
    return series, widths


@dataclass(frozen=True)
class NullSpec:
    """Random-control null configuration. Every field is an explicit config value —
    never wall-clock — so the permutation null is byte-identical across runs."""

    n_seeds: int = 5                 # random controls drawn per date
    base_seed: int = 42              # explicit seed base; seed keys are f"{base_seed+k}:{date}"
    train_frac: float = 0.0          # 0.0 disables the chronological OOS split
    alpha_t_threshold: float = 2.0   # HLZ: prefer 3.5 for whole-library scans
    min_n_eff: int = 10              # below this -> verdict "insufficient_data"


# Immutable all-defaults singleton used as the `null` default arg (frozen dataclass ->
# safe to share; a bare `NullSpec()` in the signature trips ruff B008).
_DEFAULT_NULL = NullSpec()


def _shuffled(vals: list[float], seed_key: str) -> list[float]:
    """A copy of `vals` shuffled by `random.Random(seed_key)`. String seeding is
    version-2 (sha512-based): deterministic across runs and platforms and independent
    of PYTHONHASHSEED. Copies first, so the caller's list is never mutated. A shuffle
    preserves the score multiset, so spearman on the result is defined exactly when it
    was on the input (n>=3, non-constant) — both permutation-invariant."""
    out = list(vals)
    random.Random(seed_key).shuffle(out)
    return out


def _alpha_series(sections: list[_CrossSection], ics: list[float], *,
                  n_seeds: int, base_seed: int) -> list[float]:
    """Per section i at date t: alpha_i = ics[i] - mean over k<n_seeds of
    spearman(_shuffled(vals, f"{base_seed+k}:{t}"), fwds). The shuffle permutes scores
    WITHIN one date only (the RNG is seeded by that date), never touching fwds/bars or
    other dates. Because the shuffle preserves each section's score multiset, every
    random IC is defined, so len(alpha) == len(sections) always."""
    alpha: list[float] = []
    for i, sec in enumerate(sections):
        randoms: list[float] = []
        for k in range(n_seeds):
            ric = spearman(_shuffled(sec.vals, f"{base_seed + k}:{sec.t.isoformat()}"),
                           sec.fwds)
            assert ric is not None                # shuffle preserves the defined-spearman property
            randoms.append(ric)
        alpha.append(ics[i] - statistics.fmean(randoms))
    return alpha


def _effective_n(n_periods: int, horizon: int, rebalance_every: int) -> int:
    """Count of NON-OVERLAPPING forward windows among n_periods rebalances. When
    rebalance_every < horizon the windows overlap and the IC series autocorrelates, so
    the t-stat must use independent observations, not the raw period count. Both
    arguments are counted in shared-union-calendar dates, so the stride is exact for
    every symbol regardless of its own trading week."""
    if n_periods <= 0:
        return 0
    stride = max(1, math.ceil(horizon / max(1, rebalance_every)))
    return math.ceil(n_periods / stride)


def _t_stat(series: list[float], n_eff: int) -> float:
    """t-stat of a mean-zero-null series = mean / stdev(n-1) * sqrt(n_eff), 0.0 when
    fewer than 2 samples or a zero/undefined stdev. Multiplies by the NON-OVERLAPPING
    n_eff, never the raw sample count, so overlapping forward windows do not inflate
    significance. Used for both the base IC series and the paired alpha series."""
    if len(series) < 2:
        return 0.0
    std = statistics.stdev(series)
    if not std:
        return 0.0
    return statistics.fmean(series) / std * math.sqrt(n_eff)


_MIN_SPLIT_SAMPLES = 6   # a train/test segment shorter than this -> split n/a


def _split_alpha_t(alpha: list[float], *, horizon: int, rebalance_every: int,
                   train_frac: float) -> tuple[float | None, float | None]:
    """Chronological train/test split of the EMITTED alpha series (design §4.2).
    `train_frac <= 0` disables it -> (None, None). Otherwise split at
    `k = floor(n * train_frac)`: train = alpha[:k]; the test segment drops its first
    `stride - 1` samples as an embargo (`stride = ceil(horizon / rebalance_every)`) so no
    test forward window overlaps a train window — with the default `rebalance_every ==
    horizon` the stride is 1 and nothing is dropped. Each segment is scored by its own
    `_t_stat` over its own non-overlapping `_effective_n`. If either segment ends shorter
    than `_MIN_SPLIT_SAMPLES` after the embargo the split is n/a -> (None, None), and the
    caller falls back to the full-sample verdict — never a silent partial readout."""
    if train_frac <= 0.0:
        return None, None
    n = len(alpha)
    k = math.floor(n * train_frac)
    stride = max(1, math.ceil(horizon / max(1, rebalance_every)))
    train = alpha[:k]
    test = alpha[k + stride - 1:]
    if len(train) < _MIN_SPLIT_SAMPLES or len(test) < _MIN_SPLIT_SAMPLES:
        return None, None
    t_train = _t_stat(train, _effective_n(len(train), horizon, rebalance_every))
    t_test = _t_stat(test, _effective_n(len(test), horizon, rebalance_every))
    return t_train, t_test


# Loose base gate — FIXED policy (design §4.3), not config; matches the program's legacy
# survival gate. Only alpha_t_threshold (null survival) is configurable via NullSpec.
_GATE_IC_MEAN = 0.02     # ic_mean must exceed this
_GATE_HIT = 0.55         # hit_rate must reach this
_GATE_T = 2.0            # |t_stat| must exceed this


def _verdict(*, n_eff: int, ic_mean: float, hit_rate: float, t_stat: float, alpha_t: float,
             alpha_t_test: float | None, split_ran: bool, null: NullSpec) -> str:
    """Honest verdict for a factor (design §4.3), pure and directly testable. First match
    wins:

    1. n_eff < null.min_n_eff -> "insufficient_data" (few non-overlapping windows never
       yield a confident category; n_periods == 0 collapses to n_eff == 0, the same branch).
    2. alpha_t <= -null.alpha_t_threshold -> "reversed" (significantly worse than its own
       within-date random control; subsumes the legacy ic_mean<-0.02 & |t|>2 reversed).
    3. Gate = ic_mean > 0.02 AND hit_rate >= 0.55 AND |t_stat| > 2 AND
       alpha_t >= threshold (the loose base gate AND null survival). Fail -> "noise" —
       this is the VT 12/12->1/12 case when raw IC/t pass but alpha_t falls short.
    4. Gate passed and the split ran: alpha_t_test >= thr -> "confirmed_alive";
       alpha_t_test <= -thr -> "reversed" (OOS sign flip); else -> "train_only".
    5. Gate passed, no split (off or n/a) -> "confirmed_alive".

    `alpha_t` is a plain float here: the caller passes 0.0 when n_periods == 0, but rule 1
    fires first in that case, so the value never decides the verdict."""
    thr = null.alpha_t_threshold
    if n_eff < null.min_n_eff:
        return "insufficient_data"
    if alpha_t <= -thr:
        return "reversed"
    gate = (ic_mean > _GATE_IC_MEAN and hit_rate >= _GATE_HIT
            and abs(t_stat) > _GATE_T and alpha_t >= thr)
    if not gate:
        return "noise"
    if split_ran:
        assert alpha_t_test is not None     # split_ran True <=> both split t-stats are floats
        if alpha_t_test >= thr:
            return "confirmed_alive"
        if alpha_t_test <= -thr:
            return "reversed"
        return "train_only"
    return "confirmed_alive"


def evaluate_factor(factor: Factor, history: dict[str, list[Bar]], *, horizon: int,
                    rebalance_every: int, horizons: list[int],
                    min_cross: int = 5, null: NullSpec = _DEFAULT_NULL) -> ICResult:
    # Defense in depth: a non-positive horizon/rebalance_every/horizons entry would let
    # forward_return index onto a REAL future bar — a silent look-ahead leak, not a
    # crash. The CLI now passes through a user-supplied --horizon, so this can no
    # longer be assumed to always come from trusted call sites.
    if horizon < 1 or rebalance_every < 1 or any(h < 1 for h in horizons):
        raise ValueError(
            f"evaluate_factor requires horizon >= 1, rebalance_every >= 1, and all "
            f"horizons >= 1 (got horizon={horizon}, rebalance_every={rebalance_every}, "
            f"horizons={horizons})"
        )
    # NullSpec is an unvalidated frozen dataclass; the config layer (pydantic) normally
    # guards its fields, but direct callers can pass nonsense — reject it defensively.
    if (null.n_seeds < 1 or not (0.0 <= null.train_frac < 1.0)
            or null.alpha_t_threshold <= 0.0 or null.min_n_eff < 2):
        raise ValueError(
            f"evaluate_factor requires NullSpec with n_seeds >= 1, 0 <= train_frac < 1, "
            f"alpha_t_threshold > 0, and min_n_eff >= 2 (got {null})"
        )
    calendar = union_calendar(history)
    dates = calendar[::max(1, rebalance_every)]
    # One cross-section pass feeds both the base IC series and the random-control null,
    # so factor fns are called once per (date, symbol) and the alpha series stays 1:1.
    sections = _cross_sections(factor, history, dates, horizon, min_cross, calendar)
    ic: list[float] = []
    widths: list[int] = []
    for sec in sections:
        v = spearman(sec.vals, sec.fwds)
        assert v is not None                    # _cross_sections keeps only defined ICs
        ic.append(v)
        widths.append(len(sec.vals))
    n = len(ic)
    ic_mean = statistics.fmean(ic) if ic else 0.0
    ic_std = statistics.stdev(ic) if n >= 2 else 0.0
    ir = ic_mean / ic_std if ic_std else 0.0
    n_eff = _effective_n(n, horizon, rebalance_every)   # independent (non-overlapping) samples
    t_stat = _t_stat(ic, n_eff)
    hit_rate = sum(1 for x in ic if x > 0) / n if n else 0.0
    # Random-control null: alpha_t inherits the base series' overlap and gets the SAME
    # non-overlapping n_eff (design §4.2). alpha_mean/alpha_t are None IFF n_periods == 0.
    alpha = _alpha_series(sections, ic, n_seeds=null.n_seeds, base_seed=null.base_seed)
    assert len(alpha) == n                      # every emitted date yields a defined alpha
    alpha_mean = statistics.fmean(alpha) if alpha else None
    alpha_t = _t_stat(alpha, n_eff) if alpha else None
    alpha_t_train, alpha_t_test = _split_alpha_t(
        alpha, horizon=horizon, rebalance_every=rebalance_every, train_frac=null.train_frac)
    by_h: dict[int, float | None] = {}
    for h in horizons:
        s, _ = _ic_series(factor, history, dates, h, min_cross, calendar)
        by_h[h] = statistics.fmean(s) if s else None
    breadth = int(statistics.median(widths)) if widths else 0
    # Honest verdict (design §4.3). The split ran only when it produced both segment
    # t-stats; alpha_t is None IFF n_periods == 0, where rule 1 fires on n_eff regardless,
    # so passing 0.0 in that case is safe (see _verdict docstring).
    split_ran = alpha_t_train is not None and alpha_t_test is not None
    verdict = _verdict(n_eff=n_eff, ic_mean=ic_mean, hit_rate=hit_rate, t_stat=t_stat,
                       alpha_t=alpha_t if alpha_t is not None else 0.0,
                       alpha_t_test=alpha_t_test, split_ran=split_ran, null=null)
    return ICResult(factor.name, horizon, ic_mean, ic_std, ir, t_stat, hit_rate, n,
                    by_h, breadth, alpha_mean=alpha_mean, alpha_t=alpha_t,
                    alpha_t_train=alpha_t_train, alpha_t_test=alpha_t_test, n_eff=n_eff,
                    verdict=verdict)
