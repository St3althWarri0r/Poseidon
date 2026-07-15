# Factor Research Lab (#4a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An offline factor research lab — a factor library + a point-in-time IC/IR benchmarking harness — in a new `research/` package, with a `poseidon research factors` CLI. It measures whether a factor ranks future returns; it never touches the risk/order/decision path.

**Architecture:** A pure, dependency-light core (`factors.py`, `ic.py`, `report.py`) over a pre-loaded `history: dict[str, list[Bar]]` (the shape `BacktestEngine.run` takes), plus a thin async `loader.py` and a CLI. Factors are built from the existing `strategy/indicators.py`. Stats use the stdlib `statistics` module. Design spec: `docs/superpowers/specs/2026-07-14-factor-research-lab-design.md`.

**Tech Stack:** Python stdlib (`statistics`, `math`, `datetime`), pydantic `Bar` (core/models), `DataRouter`, argparse.

## Global Constraints

- Python 3.11+, `from __future__ import annotations`, mypy `--strict`, ruff line length 100.
- **Point-in-time invariant:** a factor is handed ONLY `visible_bars(bars, t)` (bars with `end.date() <= t`); it never receives a future bar. Forward returns use bars strictly after `t`, only as the label.
- **No numpy / scipy / pandas.** Stdlib `statistics` only (`statistics.correlation` on ranks = Spearman; `statistics.stdev` sample n−1; `statistics.fmean`).
- **Pure core, no I/O:** `factors.py`/`ic.py`/`report.py` take a `history` dict and return values — no network, no disk, no `DataRouter`. Only `loader.py` and the CLI touch I/O.
- **Zero live-trading surface:** nothing here imports or is imported by `risk/`, `execution/`, or `app.py`'s decision path.
- **Honest stats:** t-stat uses NON-OVERLAPPING samples (`n_eff`), not the raw period count; `stdev` is sample (n−1); the report flags thin universes.
- `Bar` fields: `symbol, open, high, low, close` (`Decimal`), `volume: int`, `start, end: datetime`, `source`. Bars are chronological ascending.
- Gate: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest`. No `tools/ui_verify.py` (no UI).

---

### Task 1: Point-in-time + stats primitives (`research/ic.py` part 1)

**Files:**
- Create: `src/poseidon/research/__init__.py` (empty)
- Create: `src/poseidon/research/ic.py`
- Test: `tests/unit/test_research_ic_primitives.py`

**Interfaces:**
- Produces: `visible_bars(bars, as_of) -> list[Bar]`; `forward_return(bars, as_of, horizon) -> float | None`; `spearman(xs, ys) -> float | None`; `rebalance_dates(history, every) -> list[date]`.

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_research_ic_primitives.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.ic import forward_return, rebalance_dates, spearman, visible_bars


def _bars(closes: list[float], start_day: int = 1) -> list[Bar]:
    out = []
    for k, c in enumerate(closes):
        d = datetime(2026, 1, start_day + k, tzinfo=UTC)
        out.append(Bar(symbol="X", open=Decimal(str(c)), high=Decimal(str(c)),
                       low=Decimal(str(c)), close=Decimal(str(c)), volume=100,
                       start=d, end=d, source="t"))
    return out


def test_visible_bars_excludes_future() -> None:
    bars = _bars([1, 2, 3, 4, 5])          # days 1..5
    vis = visible_bars(bars, datetime(2026, 1, 3, tzinfo=UTC).date())
    assert [float(b.close) for b in vis] == [1, 2, 3]   # nothing after day 3


def test_forward_return_uses_future_label() -> None:
    bars = _bars([10, 11, 12, 13, 15])     # day1=10 ... day5=15
    # as_of day 2 (close 11), horizon 2 -> day 4 close 13 -> 13/11 - 1
    assert forward_return(bars, datetime(2026, 1, 2, tzinfo=UTC).date(), 2) == 13 / 11 - 1
    # horizon past the end -> None
    assert forward_return(bars, datetime(2026, 1, 4, tzinfo=UTC).date(), 5) is None


def test_spearman_monotonic_and_guards() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0      # perfectly rank-correlated
    assert round(spearman([1, 2, 3, 4], [40, 30, 20, 10]), 6) == -1.0
    assert spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None         # constant -> None
    assert spearman([1, 2], [1, 2]) is None                     # n < 3 -> None


def test_rebalance_dates_strides() -> None:
    hist = {"X": _bars([1, 2, 3, 4, 5, 6])}
    ds = rebalance_dates(hist, 2)
    assert ds[0].day == 1 and ds[1].day == 3 and ds[2].day == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_research_ic_primitives.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'poseidon.research'`.

- [ ] **Step 3: Write minimal implementation**

`src/poseidon/research/__init__.py`: empty.
`src/poseidon/research/ic.py`:
```python
"""Point-in-time IC/IR benchmarking. Pure — no I/O. A factor is handed only the
sliced past window, so look-ahead leakage is impossible at the factor boundary."""
from __future__ import annotations

import math
import statistics
from datetime import date

from ..core.models import Bar


def visible_bars(bars: list[Bar], as_of: date) -> list[Bar]:
    """Bars with end.date() <= as_of (bars are chronological ascending)."""
    return [b for b in bars if b.end.date() <= as_of]


def forward_return(bars: list[Bar], as_of: date, horizon: int) -> float | None:
    """Return over the `horizon` bars AFTER the last bar visible at as_of. The
    factor's last bar (index i) is the base; bars[i+horizon] is the future label."""
    vis = visible_bars(bars, as_of)
    if not vis:
        return None
    i = len(vis) - 1
    if i + horizon >= len(bars):
        return None
    base = float(bars[i].close)
    if base == 0.0:
        return None
    return float(bars[i + horizon].close) / base - 1.0


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


def rebalance_dates(history: dict[str, list[Bar]], every: int) -> list[date]:
    """Every `every`-th distinct trading date across the whole universe."""
    dates = sorted({b.end.date() for bars in history.values() for b in bars})
    step = max(1, every)
    return dates[::step]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_research_ic_primitives.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/research/__init__.py src/poseidon/research/ic.py tests/unit/test_research_ic_primitives.py
git commit -m "feat(research): point-in-time + spearman primitives for factor IC"
```

---

### Task 2: Factor abstraction + starter library (`research/factors.py`)

**Files:**
- Create: `src/poseidon/research/factors.py`
- Test: `tests/unit/test_research_factors.py`

**Interfaces:**
- Consumes: `Bar`; `strategy/indicators.py` (`cumulative_return`, `rate_of_change`, `stdev_return`, `rsi`, `sma`, `max_drawdown`, `highest` — all `(...) -> float | None`).
- Produces: `Factor(name, fn, description="", min_bars=2)`; `ALL_FACTORS: list[Factor]`.

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_research_factors.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.factors import ALL_FACTORS, Factor


def _series(closes: list[float]) -> list[Bar]:
    out = []
    for k, c in enumerate(closes):
        d = datetime(2024, 1, 1, tzinfo=UTC)
        out.append(Bar(symbol="X", open=Decimal(str(c)), high=Decimal(str(c * 1.01)),
                       low=Decimal(str(c * 0.99)), close=Decimal(str(c)), volume=100 + k,
                       start=d, end=d, source="t"))
    return out


def test_all_factors_have_unique_names() -> None:
    names = [f.name for f in ALL_FACTORS]
    assert len(names) == len(set(names)) and len(names) >= 12


def test_factors_return_float_or_none() -> None:
    rising = _series([100 + i for i in range(300)])   # long rising series
    for f in ALL_FACTORS:
        v = f.fn(rising)
        assert v is None or isinstance(v, float)
        assert f.fn(_series([100.0])) is None          # too short -> None (min_bars)


def test_momentum_positive_on_uptrend() -> None:
    rising = _series([100 * (1.005 ** i) for i in range(300)])
    mom = next(f for f in ALL_FACTORS if f.name == "momentum_6m")
    assert mom.fn(rising) is not None and mom.fn(rising) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_research_factors.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`src/poseidon/research/factors.py`:
```python
"""Starter alpha-factor library. Each Factor maps ONE symbol's point-in-time bars
to a cross-sectional score (higher = more attractive), or None if it can't be
computed from the given window. Pure; built on strategy/indicators.py."""
from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import Bar
from ..strategy.indicators import (
    cumulative_return, highest, max_drawdown, rate_of_change, rsi, sma, stdev_return)


@dataclass(frozen=True)
class Factor:
    name: str
    fn: Callable[[list[Bar]], float | None]
    description: str = ""
    min_bars: int = 2


def _closes(bars: list[Bar]) -> list[float]:
    return [float(b.close) for b in bars]


def _vols(bars: list[Bar]) -> list[int]:
    return [b.volume for b in bars]


def _momentum_12_1(bars: list[Bar]) -> float | None:
    c = _closes(bars)
    if len(c) < 253:
        return None
    return c[-22] / c[-253] - 1.0            # 12 months ago -> 1 month ago (skip last 21d)


def _vol_scaled_mom(bars: list[Bar]) -> float | None:
    m = cumulative_return(_closes(bars), 126)
    s = stdev_return(_closes(bars), 20)
    if m is None or not s:
        return None
    return m / s


def _trend_vs_sma50(bars: list[Bar]) -> float | None:
    s = sma(_closes(bars), 50)
    if not s:
        return None
    return _closes(bars)[-1] / s - 1.0


def _drawdown_60(bars: list[Bar]) -> float | None:
    md = max_drawdown(_closes(bars), 60)
    return None if md is None else -abs(md)  # less drawdown preferred


def _volume_ratio(bars: list[Bar]) -> float | None:
    v = _vols(bars)
    if len(v) < 40:
        return None
    base = statistics.fmean(v[-40:-20])
    if base == 0:
        return None
    return statistics.fmean(v[-20:]) / base - 1.0


def _near_52w_high(bars: list[Bar]) -> float | None:
    hi = highest(_closes(bars), 252)
    if not hi:
        return None
    return _closes(bars)[-1] / hi


def _neg(fn: Callable[..., float | None], *a: object) -> Callable[[list[Bar]], float | None]:
    def inner(bars: list[Bar]) -> float | None:
        v = fn(_closes(bars), *a)
        return None if v is None else -v
    return inner


def _pos(fn: Callable[..., float | None], *a: object) -> Callable[[list[Bar]], float | None]:
    def inner(bars: list[Bar]) -> float | None:
        return fn(_closes(bars), *a)
    return inner


def _rsi_reversion(bars: list[Bar]) -> float | None:
    r = rsi(_closes(bars), 14)
    return None if r is None else 50.0 - r    # overbought -> lower score


ALL_FACTORS: list[Factor] = [
    Factor("momentum_12_1", _momentum_12_1, "12m return skipping last month", min_bars=253),
    Factor("momentum_6m", _pos(cumulative_return, 126), "6-month return", min_bars=127),
    Factor("momentum_3m", _pos(cumulative_return, 63), "3-month return", min_bars=64),
    Factor("reversal_1m", _neg(cumulative_return, 21), "negated 1-month return", min_bars=22),
    Factor("reversal_5d", _neg(rate_of_change, 5), "negated 5-day return", min_bars=6),
    Factor("low_vol_20d", _neg(stdev_return, 20), "negated 20d return vol", min_bars=21),
    Factor("rsi_reversion_14", _rsi_reversion, "50 - RSI(14)", min_bars=15),
    Factor("trend_vs_sma50", _trend_vs_sma50, "close/SMA50 - 1", min_bars=51),
    Factor("vol_scaled_mom_6m", _vol_scaled_mom, "6m return / 20d vol", min_bars=127),
    Factor("drawdown_60d", _drawdown_60, "negated |max drawdown| over 60d", min_bars=61),
    Factor("volume_ratio_20", _volume_ratio, "recent/base 20d volume - 1", min_bars=40),
    Factor("near_52w_high", _near_52w_high, "close / 252d high", min_bars=252),
]
```
(If a helper's exact signature differs, adapt the call — the contract is `fn(bars) -> float | None`. Verify each `indicators` helper's arg order against `strategy/indicators.py` before finalizing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_research_factors.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/research/factors.py tests/unit/test_research_factors.py
git commit -m "feat(research): starter alpha-factor library"
```

---

### Task 3: IC/IR evaluation with non-overlap t-stat + decay (`research/ic.py` part 2)

**Files:**
- Modify: `src/poseidon/research/ic.py` (add `ICResult`, `_ic_series`, `evaluate_factor`)
- Test: `tests/unit/test_research_evaluate.py`

**Interfaces:**
- Consumes: `Factor` (Task 2); the primitives (Task 1).
- Produces: `ICResult(factor, horizon, ic_mean, ic_std, ir, t_stat, hit_rate, n_periods, ic_by_horizon)`; `evaluate_factor(factor, history, *, horizon, rebalance_every, horizons, min_cross=5) -> ICResult`.

- [ ] **Step 1: Write the failing test** (the invariant tests — anti-lookahead probe, non-circular IC≈+1, non-overlap n_eff)
```python
# tests/unit/test_research_evaluate.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_research_evaluate.py -q`
Expected: FAIL — `ImportError: cannot import name 'evaluate_factor'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/poseidon/research/ic.py` (add imports `from dataclasses import dataclass`, `from .factors import Factor`):
```python
@dataclass(frozen=True)
class ICResult:
    factor: str
    horizon: int
    ic_mean: float
    ic_std: float
    ir: float
    t_stat: float
    hit_rate: float
    n_periods: int
    ic_by_horizon: dict[int, float]


def _ic_series(factor: Factor, history: dict[str, list[Bar]], dates: list[date],
               horizon: int, min_cross: int) -> list[float]:
    series: list[float] = []
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
            r = forward_return(bars, t, horizon)
            if r is None:
                continue
            vals.append(v)
            fwds.append(r)
        if len(vals) >= min_cross:
            ic = spearman(vals, fwds)
            if ic is not None:
                series.append(ic)
    return series


def _effective_n(n_periods: int, horizon: int, rebalance_every: int) -> int:
    """Count of NON-OVERLAPPING forward windows among n_periods rebalances. When
    rebalance_every < horizon the windows overlap and the IC series autocorrelates, so
    the t-stat must use independent observations, not the raw period count."""
    if n_periods <= 0:
        return 0
    stride = max(1, math.ceil(horizon / max(1, rebalance_every)))
    return math.ceil(n_periods / stride)


def evaluate_factor(factor: Factor, history: dict[str, list[Bar]], *, horizon: int,
                    rebalance_every: int, horizons: list[int],
                    min_cross: int = 5) -> ICResult:
    dates = rebalance_dates(history, rebalance_every)
    ic = _ic_series(factor, history, dates, horizon, min_cross)
    n = len(ic)
    ic_mean = statistics.fmean(ic) if ic else 0.0
    ic_std = statistics.stdev(ic) if n >= 2 else 0.0
    ir = ic_mean / ic_std if ic_std else 0.0
    n_eff = _effective_n(n, horizon, rebalance_every)   # independent (non-overlapping) samples
    t_stat = ir * math.sqrt(n_eff) if n_eff else 0.0
    hit_rate = sum(1 for x in ic if x > 0) / n if n else 0.0
    by_h: dict[int, float] = {}
    for h in horizons:
        s = _ic_series(factor, history, dates, h, min_cross)
        by_h[h] = statistics.fmean(s) if s else 0.0
    return ICResult(factor.name, horizon, ic_mean, ic_std, ir, t_stat, hit_rate, n, by_h)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_research_evaluate.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/research/ic.py tests/unit/test_research_evaluate.py
git commit -m "feat(research): IC/IR evaluation with non-overlap t-stat + decay"
```

---

### Task 4: Factor report (`research/report.py`)

**Files:**
- Create: `src/poseidon/research/report.py`
- Test: `tests/unit/test_research_report.py`

**Interfaces:**
- Consumes: `ALL_FACTORS`/`Factor`, `evaluate_factor`/`ICResult`.
- Produces: `FactorReport(results: list[ICResult], cross_section_size: int, thin: bool)`; `run_report(factors, history, *, horizon, rebalance_every, horizons, min_cross=5) -> FactorReport` (sorted by `abs(t_stat)` desc); `FactorReport.render() -> str`.

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_research_report.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.factors import ALL_FACTORS
from poseidon.research.report import run_report


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_research_report.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`src/poseidon/research/report.py`:
```python
"""Run a factor set over a universe and rank by |t-stat|. Descriptive point-in-time
IC over the supplied history — NOT tradable PnL, and noisy on a thin universe."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Bar
from .factors import Factor
from .ic import ICResult, evaluate_factor

_THIN_UNIVERSE = 20     # cross-sectional IC below this many names is very noisy


@dataclass(frozen=True)
class FactorReport:
    results: list[ICResult]
    cross_section_size: int
    thin: bool

    def render(self) -> str:
        head = (f"Factor IC/IR report — universe {self.cross_section_size} symbols"
                + ("  [THIN: results are noisy/unreliable]" if self.thin else ""))
        cols = f"{'factor':<20} {'IC':>8} {'IR':>8} {'t-stat':>8} {'hit':>6}  decay"
        rows = []
        for r in self.results:
            decay = " ".join(f"{h}:{v:+.3f}" for h, v in sorted(r.ic_by_horizon.items()))
            rows.append(f"{r.factor:<20} {r.ic_mean:>+8.4f} {r.ir:>+8.3f} "
                        f"{r.t_stat:>+8.2f} {r.hit_rate:>6.2f}  {decay}")
        note = ("Descriptive point-in-time IC; not tradable PnL (no costs/capacity); "
                "mining many factors on one universe overfits.")
        return "\n".join([head, "", cols, *rows, "", note])


def run_report(factors: list[Factor], history: dict[str, list[Bar]], *, horizon: int,
               rebalance_every: int, horizons: list[int], min_cross: int = 5) -> FactorReport:
    results = [evaluate_factor(f, history, horizon=horizon, rebalance_every=rebalance_every,
                               horizons=horizons, min_cross=min_cross) for f in factors]
    results.sort(key=lambda r: abs(r.t_stat), reverse=True)
    size = len(history)
    return FactorReport(results=results, cross_section_size=size, thin=size < _THIN_UNIVERSE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_research_report.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/research/report.py tests/unit/test_research_report.py
git commit -m "feat(research): factor report — rank by |t-stat|, flag thin universe"
```

---

### Task 5: History loader (`research/loader.py`)

**Files:**
- Create: `src/poseidon/research/loader.py`
- Test: `tests/unit/test_research_loader.py`

**Interfaces:**
- Consumes: an object with `async bars(symbol, *, timeframe, limit) -> list[Bar]` (the `DataRouter` shape).
- Produces: `async load_history(router, symbols, days) -> dict[str, list[Bar]]` (skips symbols that error or return too little, logs a note).

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_research_loader.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.core.models import Bar
from poseidon.research.loader import load_history


class _Router:
    async def bars(self, symbol, *, timeframe="1d", limit=100):
        if symbol == "BAD":
            raise RuntimeError("no data")
        d = datetime(2024, 1, 1, tzinfo=UTC)
        return [Bar(symbol=symbol, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
                    close=Decimal("1"), volume=1, start=d, end=d, source="t")]


async def test_load_history_skips_failures() -> None:
    hist = await load_history(_Router(), ["AAA", "BAD", "BBB"], days=30)
    assert set(hist) == {"AAA", "BBB"}              # BAD skipped, no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_research_loader.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`src/poseidon/research/loader.py`:
```python
"""Thin async history loader for the research CLI (the only I/O in research/)."""
from __future__ import annotations

from typing import Any

import structlog

from ..core.models import Bar

log = structlog.get_logger(__name__)


async def load_history(router: Any, symbols: list[str], days: int) -> dict[str, list[Bar]]:
    hist: dict[str, list[Bar]] = {}
    for symbol in symbols:
        try:
            bars = await router.bars(symbol, timeframe="1d", limit=days)
        except Exception as exc:                    # a bad symbol must not abort the run
            log.warning("history load failed", symbol=symbol, error=str(exc))
            continue
        if bars:
            hist[symbol] = bars
    return hist
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_research_loader.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/research/loader.py tests/unit/test_research_loader.py
git commit -m "feat(research): async history loader for the CLI"
```

---

### Task 6: `ResearchConfig` + `poseidon research factors` CLI

**Files:**
- Modify: `src/poseidon/core/config.py` (add `ResearchConfig`; add `research: ResearchConfig` to `AppConfig`)
- Modify: `src/poseidon/cli.py` (`cmd_research` + subparser)
- Test: `tests/unit/test_research_cli.py`

**Interfaces:**
- Produces: `ResearchConfig(horizon=5, rebalance_every=5, horizons=[1,5,10,20], min_cross=5, lookback_days=400)`; a `research` subcommand with a `factors` action.

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_research_cli.py
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
```
(If the parser builder has a different name than `build_parser`, adjust the import to the real one in `cli.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_research_cli.py -q`
Expected: FAIL — `ImportError: cannot import name 'ResearchConfig'`.

- [ ] **Step 3: Write minimal implementation**

In `core/config.py`, add `ResearchConfig(StrictModel)` with the fields above (`horizons: list[int] = [1,5,10,20]`, all `Field(..., ge=1)` where sensible) and `research: ResearchConfig = Field(default_factory=ResearchConfig)` on `AppConfig`.
In `cli.py`, add:
```python
def cmd_research(args: argparse.Namespace) -> int:
    import asyncio
    from .research.factors import ALL_FACTORS
    from .research.loader import load_history
    from .research.report import run_report
    # Build the config + router the same way cmd_cycle/cmd_run do (load AppConfig,
    # construct the DataRouter via the kernel's _build_router, unlocking the vault
    # only if a provider needs a secret). Resolve the universe:
    cfg = _load_config()                         # reuse the existing config loader
    symbols = (args.symbols.split(",") if args.symbols
               else cfg.all_watchlist_symbols() if args.watchlist else [])
    if not symbols:
        print("provide --symbols A,B,... (a broad universe) or --watchlist")
        return 2
    async def _go() -> int:
        router = _build_research_router(cfg)     # a DataRouter over configured providers
        hist = await load_history(router, symbols, args.days or cfg.research.lookback_days)
        if len(hist) < 2:
            print("not enough symbols with history to compute cross-sectional IC")
            return 1
        rep = run_report(ALL_FACTORS, hist, horizon=args.horizon or cfg.research.horizon,
                         rebalance_every=args.rebalance_every or cfg.research.rebalance_every,
                         horizons=cfg.research.horizons, min_cross=cfg.research.min_cross)
        print(rep.render())
        return 0
    return asyncio.run(_go())
```
Register the subparser (near the other `sub.add_parser(...)` calls):
```python
    research = sub.add_parser("research", help="offline factor research")
    research_sub = research.add_subparsers(dest="research_action", required=True)
    fac = research_sub.add_parser("factors", help="rank factors by point-in-time IC/IR")
    fac.add_argument("--symbols", default="")
    fac.add_argument("--watchlist", action="store_true")
    fac.add_argument("--days", type=int, default=0)
    fac.add_argument("--horizon", type=int, default=0)
    fac.add_argument("--rebalance-every", dest="rebalance_every", type=int, default=0)
    fac.set_defaults(func=cmd_research)
```
Refactor the parser construction into a `build_parser()` function if it isn't one already (the test imports it), and factor the small `_load_config`/`_build_research_router` helpers out of the existing `cmd_run`/`cmd_cycle` construction (do NOT duplicate the kernel; reuse `ApplicationKernel._build_router` by constructing a kernel and calling it, or a thin equivalent). The CLI path is not unit-tested against the network; the test only checks parser wiring + config.

- [ ] **Step 4: Run the gate**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest -q`
Expected: ruff clean, mypy `Success`, all pass.

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/core/config.py src/poseidon/cli.py tests/unit/test_research_cli.py
git commit -m "feat(cli): poseidon research factors + ResearchConfig"
```

---

### Task 7: Docs + example config

**Files:**
- Modify: `config/poseidon.example.yaml` (a commented `research:` block)
- Modify: `docs/api-configuration.md` OR a new `docs/research.md` (describe the lab)
- Test: none (docs) — run the full gate to confirm no regression.

- [ ] **Step 1: Add the commented example** under a new top-level `research:` in `config/poseidon.example.yaml`:
```yaml
# Offline factor research (poseidon research factors). Pure analysis — never
# trades. Point-in-time IC/IR; give it a BROAD --symbols universe (hundreds of
# names), not just your watchlist, or the cross-sectional IC is noisy.
research:
  horizon: 5            # forward-return horizon (trading days) for the headline IC
  rebalance_every: 5    # evaluate every N days; keep >= horizon to avoid overlap inflation
  horizons: [1, 5, 10, 20]   # decay curve
  min_cross: 5          # minimum symbols per cross-section
  lookback_days: 400    # bars to load per symbol
```

- [ ] **Step 2: Add a docs section** ("Factor research lab") describing: what IC/IR is, the point-in-time guarantee, the non-overlap t-stat, the thin-universe caveat, that it never touches live trading, and an example command.

- [ ] **Step 3: Run the full gate**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**
```bash
git add config/poseidon.example.yaml docs/
git commit -m "docs(research): document the factor research lab + example config"
```

---

## After the plan

Final whole-branch review (most-capable model) focused on the **point-in-time / non-overlap** correctness (the one way this is silently wrong) and the pure-core / no-live-surface boundary. Then release **v2.11.0** (independent of the merged tiering/packet work): bump the three version files, PR `feat/factor-research-lab` → main, merge → tag → GitHub release (fresh token + explicit sign-off). Sub-project **#4b (strategy-decay tracking)** follows, reusing `ic.py`'s rolling-IC math.
