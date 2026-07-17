# Factor Bench Rigor (random-control null gate + quantile layering) — Design Spec

**Date:** 2026-07-16 · **Status:** Approved (design), pending implementation plan
**Target:** branch `feat/r2-wave1` (Poseidon v2.13.0 candidate)
**Origin:** Cross-pollination round 2, rank 2 (`program.md` §2, `[M]` `[VT]`), extending the
round-1 factor lab spec (2026-07-14). Reference: Vibe-Trading `bench_runner_strict.py` +
`factor_analysis_core.py` (MIT), re-derived pure-stdlib.

## 1. Goal

VT's own bench showed 12/12 factors passing raw IC and only 1/12 surviving a same-universe random
control. Add that control to `poseidon research factors` — per-date permutation null with a paired
`alpha_IC` t-stat, honest verdicts, optional chronological train/test split, quantile-bucket NAV
layering, a bundled broad universe — preserving every existing invariant (pure, offline,
stdlib-only, seeded-deterministic, anti-lookahead, n−1 stdev, non-overlap `n_eff`).

## 2. Non-goals

- No live wiring: verdicts never reach the PM, workshop, risk, or order path
  (`test_research_isolation.py` stays the enforcement; only `poseidon.cli` consumes research/).
- No numpy/scipy/pandas; no HTML report; no factor optimization; no implemented multiple-testing
  *correction* (HLZ is a printed note); no point-in-time universe membership (bundled list is a
  labeled snapshot of current members).
- No change to base-stat conventions: `ic_mean`/`ir`/`t_stat` keep documented 0.0-placeholder
  semantics when `n_periods == 0`.

## 3. Program-plan corrections (verified against the code)

Base flow (ic.py): union-calendar rebalance grid → per-date cross-section (point-in-time score,
shared-calendar forward return) → Spearman IC series → mean/stdev(n−1)/IR/hit →
`t_stat = ir·sqrt(n_eff)`, `n_eff = ceil(n / ceil(horizon/rebalance_every))` (non-overlap count).
1. **The `ic_by_horizon` 0.0/no-data conflation is ALREADY FIXED** (research-math audit finding
   14): ic.py emits `None`, report.py renders `h:-`, locked by test_audit_research.py::
   `test_no_data_factor_renders_distinctly_from_zero_ic`. Not re-fixed here; this spec extends
   the same None-sentinel discipline to every new field (§4.6).
2. VT's paired t-stat uses raw `n`; Poseidon deliberately diverges: the same non-overlap `n_eff`
   applies to the alpha series (§4.2 — it inherits the overlap).

## 4. Design

Files: `research/ic.py` (null, split, verdict) · `research/groups.py` (new, layering) ·
`research/report.py` · `research/data/sp500.txt` (new) · `core/config.py` · `cli.py`
(`--train-frac`, `--universe`, symbols-file comments). `factors.py`/`loader.py` untouched.

### 4.1 Within-date random-control null (ic.py)

Refactor `_ic_series` around an explicit per-date cross-section so base IC and null share one
pass — factor fns are called once per date; the null permutes *scores*, never bars:

```python
@dataclass(frozen=True)
class _CrossSection:
    t: date
    vals: list[float]      # factor scores, index-aligned with fwds
    fwds: list[float]      # shared-calendar forward returns

def _cross_sections(factor, history, dates, horizon, min_cross, calendar) -> list[_CrossSection]
    # exactly today's _ic_series gating (min_bars, None score/forward, >= min_cross), keeping a
    # section only if spearman(vals, fwds) is not None -> sections == emitted IC samples, 1:1.
@dataclass(frozen=True)
class NullSpec:
    n_seeds: int = 5            # random controls per date
    base_seed: int = 42         # explicit config seed — never wall-clock
    train_frac: float = 0.0     # 0.0 disables the chronological split
    alpha_t_threshold: float = 2.0   # HLZ: prefer 3.5 for whole-library scans
    min_n_eff: int = 10         # below this -> verdict "insufficient_data"
def _shuffled(vals: list[float], seed_key: str) -> list[float]
    # copy + random.Random(seed_key).shuffle(copy); str seeding is version-2 (sha512-based),
    # deterministic across runs/platforms, PYTHONHASHSEED-independent.
def _alpha_series(sections, ics, *, n_seeds, base_seed) -> list[float]
    # per section i at date t, for k < n_seeds:
    #   random_ic_k = spearman(_shuffled(vals, f"{base_seed + k}:{t.isoformat()}"), fwds)
    #   alpha_i = ics[i] - fmean(random_ic_k)
```

**Within-date only, by construction:** each date's shuffle uses an RNG seeded by
`(base_seed + k, that date)` — scores never move across dates, and per-date permutations are
independent of other dates' cross-section sizes. **No inner-join needed (simpler than VT):** a
shuffle preserves the score multiset, and `spearman` is None only for n<3 or a constant vector —
both permutation-invariant — so every emitted base-IC date yields all `n_seeds` valid random ICs
and `len(alpha_series) == n_periods` always (assert in implementation).

### 4.2 Paired t-stat, overlap inheritance, train/test split (ic.py)

`_t_stat(series: list[float], n_eff: int) -> float` = `fmean/stdev(n−1) · sqrt(n_eff)` (0.0 when
n<2 or std==0); `alpha_t = _t_stat(alpha_series, _effective_n(len(alpha_series), horizon,
rebalance_every))`.
**The alpha pairs inherit the base series' overlap** — state it in the docstring: when
`rebalance_every < horizon`, consecutive `IC_t` share forward windows and autocorrelate;
subtracting `random_IC_t` (per-date permutation ICs — serially independent, ≈0 mean) adds noise
but cannot remove that dependence, so the paired series autocorrelates exactly like the base
series and gets the same `n_eff` (skipped min_cross dates keep it conservative, as today).

**Split (default off):** `train_frac > 0` splits the *emitted* alpha series chronologically at
`k = floor(n · train_frac)`. **Embargo:** the test segment drops its first `stride − 1` samples
(`stride = ceil(horizon / rebalance_every)`) so no test window overlaps a train window (default
`rebalance_every == horizon` → stride 1 → nothing dropped). Each segment gets its own `_t_stat`
with its own `_effective_n(len(segment), …)`. If either segment ends `< _MIN_SPLIT_SAMPLES = 6`
after the embargo → split n/a: `alpha_t_train = alpha_t_test = None`, verdict falls back to
full-sample logic, report shows the split didn't run — never silent.

### 4.3 Verdicts (ic.py; stored on ICResult, rendered by report.py)

Module constants `_GATE_IC_MEAN = 0.02`, `_GATE_HIT = 0.55`, `_GATE_T = 2.0` (fixed policy, not
config); `alpha_t_threshold` configurable (default 2.0). Pure, directly testable; first match wins:
```python
def _verdict(*, n_eff: int, ic_mean: float, hit_rate: float, t_stat: float, alpha_t: float,
             alpha_t_test: float | None, split_ran: bool, null: NullSpec) -> str
```
1. `n_periods == 0 or n_eff < null.min_n_eff` → **insufficient_data** — few dates never yield a
   confident category.
2. `alpha_t <= -null.alpha_t_threshold` → **reversed** (significantly worse than its own random
   control; subsumes the legacy `ic_mean < −0.02 & |t| > 2` reversed).
3. Gate = `ic_mean > 0.02 and hit_rate >= 0.55 and abs(t_stat) > 2 and alpha_t >= threshold` —
   the program's loose gate AND null survival. Fail → **noise**.
4. Gate passed, split ran: `alpha_t_test >= thr` → **confirmed_alive**; `<= -thr` → **reversed**
   (OOS sign flip); else → **train_only**.
5. Gate passed, no split (off or n/a) → **confirmed_alive**.

### 4.4 Quantile layering (new research/groups.py)

```python
@dataclass(frozen=True)
class GroupEquityResult:
    factor: str
    n_groups: int
    n_periods: int                # emitted rebalance intervals
    breadth: int                  # median per-date cross-section (0 if none)
    dates: list[date]             # interval start dates
    nav: list[list[float]]        # nav[g] from 1.0; g=0 lowest score … n_groups-1 highest
    total_return: list[float]     # nav[g][-1] - 1.0
    long_short: float | None     # total_return[-1] - total_return[0]; None when insufficient
    mono_rho: float | None       # spearman(bucket idx, total_return); None if n_groups<3/insufficient
    monotonic: bool | None       # totals strictly increasing; None when insufficient
    insufficient: bool
    reason: str                   # "" or e.g. "6 periods < 12" / "breadth 8 < 15"
def compute_group_equity(factor: Factor, history: dict[str, list[Bar]], *, n_groups: int = 5,
                         rebalance_every: int, min_cross: int = 5) -> GroupEquityResult
```

Per rebalance date on the same union-calendar grid: cross-section of (score,
**hold-to-next-rebalance return**) = `forward_return(bars, t, horizon=rebalance_every, calendar)`,
landing exactly on the next grid date — consecutive NAV periods are contiguous and non-overlapping
regardless of the IC horizon (compounding overlapping IC-horizon returns would be wrong; document).
A date emits only with `>= max(min_cross, n_groups)` paired names (every bucket non-empty; no VT
filler). Bucketing: sort by `(score, symbol)` ascending (deterministic tie-break), assign
`g = i * n_groups // n` (sizes ≤1 apart), equal-weight `fmean` per bucket; NAV compounds from 1.0.

**Insufficient-data output (never confident readouts on thin data):** constants
`_GROUP_MIN_PERIODS = 12`, `_GROUP_MIN_PER_BUCKET = 3`; if `n_periods < 12` or
`breadth < 3 * n_groups` → `insufficient=True`, `long_short/mono_rho/monotonic = None`, `reason`
set; NAV data still returned; report prints `insufficient data (<reason>)` instead of the readout.
Imports only `.factors`, `.ic`, `..core.models`, stdlib (isolation allowlist holds).

### 4.5 Bundled universe + CLI plumbing (cli.py, research/data/sp500.txt)

- `src/poseidon/research/data/sp500.txt`: ~500 current S&P 500 constituents, one per line,
  dot-class tickers kept (`BRK.B`); `#` header comments carry `source:`, `as_of: 2026-07`, and the
  survivorship warning. Hatch wheel (`packages = ["src/poseidon"]`) already ships non-.py package
  files — verify in the built wheel; no pyproject change.
- `_research_symbols`: skip `#`-comment lines (today they become bogus symbols that fail at load);
  order-preserving dedupe. Applies to user files too — document in `--symbols-file` help.
- New flag `--universe sp500` (choices, default ""); precedence `--symbols > --symbols-file >
  --universe > --watchlist`; resolved at the CLI edge via
  `importlib.resources.files("poseidon.research") / "data" / "sp500.txt"` — research/ never reads
  it itself; loader.py unchanged. With `--universe`, `cmd_research` passes `universe_note` to
  `run_report` → **labeled** caveat in the report (§4.8), never silent.

### 4.6 No-data sentinels on new fields (the finding-14 discipline)

`ICResult` gains appended, keyword-constructed fields: `alpha_mean: float | None = None` ·
`alpha_t: float | None = None` · `alpha_t_train/alpha_t_test: float | None = None` ·
`n_eff: int = 0` · `verdict: str = "insufficient_data"`. `alpha_mean`/`alpha_t` are `None` iff
`n_periods == 0` (never-evaluated ≠ measured 0.0); train/test `None` when the split is off or n/a;
report renders `None` as `-`, like decay cells. Base fields keep the 0.0-placeholder convention.

### 4.7 Config & signatures

`ResearchConfig` additions (core/config.py): `null_seeds: int = 5 (ge=1)` ·
`null_base_seed: int = 42` · `train_frac: float = 0.0 (ge=0, lt=1)`
· `alpha_t_threshold: float = 2.0 (gt=0)` · `verdict_min_n_eff: int = 10 (ge=2)` ·
`n_groups: int = 5 (ge=2)`. CLI: `--train-frac` (new `_unit_fraction` argparse type: float,
`0 <= v < 1`; default None → config) and `--universe` (§4.5); seeds/groups/threshold config-only.

```python
def evaluate_factor(factor, history, *, horizon: int, rebalance_every: int, horizons: list[int],
                    min_cross: int = 5, null: NullSpec = NullSpec()) -> ICResult
    # defensive ValueError (same style as the horizon guard) on bad NullSpec values
def run_report(factors, history, *, horizon: int, rebalance_every: int, horizons: list[int],
               min_cross: int = 5, null: NullSpec = NullSpec(), n_groups: int = 5,
               universe_note: str = "") -> FactorReport
```
`cmd_research` builds `NullSpec` from `config.research` (CLI `--train-frac` overrides);
`FactorReport` gains `groups: list[GroupEquityResult]`, `null`, `split_ran`, `universe_note`.

### 4.8 Report text additions (report.py)

- Main table gains, before the decay cell: `{alpha_mean:>+9.4f} {alpha_t:>+8.2f}  {verdict:<15}`
  (header `alphaIC alpha_t verdict`); rows stay factor-name-first and space-delimited (tests key
  on that); the `n_periods == 0` "no data" row is unchanged. When the split ran, a block
  `OOS split (train_frac=X.XX, embargo=<stride-1> samples):` lists per-factor
  `alpha_t_train` / `alpha_t_test` (`-` for n/a).
- Quantile section `Quantile layering (n_groups=N, hold = rebalance interval)`; per factor either
  `G1 -3.2% … G5 +8.1% | L/S +11.3% | mono rho +0.90` or `insufficient data (breadth 8 < 15)`.
- Footer notes (labeled): (1) `alpha_IC = per-date IC minus mean of N seeded within-date score
  permutations; alpha_t uses the same non-overlapping n_eff as the base t-stat.` (2) `Gate:
  ic_mean > 0.02, hit >= 0.55, |t| > 2, alpha_t >= <thr> (single-test). Harvey-Liu-Zhu (2016):
  factors found by scanning many candidates should clear |t| >= 3.5 — treat confirmed_alive below
  that as provisional when ranking the whole library.` (3) only with `universe_note`: `UNIVERSE
  CAVEAT [sp500]: current-membership snapshot (as_of 2026-07) — survivorship bias: delisted
  members are absent; IC over today's survivors overstates efficacy.` (4) existing notes stay.

## 5. Failure modes

- Constant cross-section or n<3 → date skipped for base AND null identically (§4.1) — the alpha
  series can never misalign with the IC series.
- `rebalance_every < horizon` → both t-stats shrink via `n_eff`; the embargo widens with stride.
- Split segment too small after embargo → split n/a (`None`s + note), full-sample verdict.
- Few dates / `n_eff < verdict_min_n_eff` → `insufficient_data`; thin buckets or `< 12` periods in
  groups → `insufficient` + reason — never a confident category/readout on thin data.
- Zero close inside a hold window → return −1.0, bucket NAV floors at ~0 and stays (offline
  diagnostic; no guard beyond the existing `base == 0.0 → None` in `forward_return`).
- Bad symbols in sp500.txt (renames, per-provider dot-class format) → loader warning + skip;
  breadth/thin flag reflects what actually loaded. Bad NullSpec/config → pydantic rejects at
  parse; `evaluate_factor` raises `ValueError` defensively for direct callers.

## 6. Determinism & purity checklist (reviewers)

- [ ] No module-level `random` calls; every RNG is `random.Random(f"{seed}:{date}")` — string
      seeding, PYTHONHASHSEED-independent; reruns byte-identical; no wall-clock anywhere.
- [ ] Permutation shuffles **scores within one date only**; factor fns still receive only
      `visible_bars(bars, t)` — the null never touches bars, windows, or other dates.
- [ ] Group NAV compounds only hold-to-next-rebalance returns (contiguous, non-overlapping);
      `statistics.stdev` (n−1) everywhere; every t-stat multiplies by `sqrt(n_eff)`, never raw n.
- [ ] research/ imports stay inside the isolation allowlist (groups.py included); only cli.py
      imports research/ or reads the bundled file; loader.py remains the only other I/O edge.
- [ ] None sentinels for never-computed values (alpha, split, group readouts) — no new 0.0
      conflation; `Decimal → float` only inside factor/stat math.

## 7. TDD task list (ordered; named tests written first in every task)

1. **Cross-section refactor + null core** (`ic.py`; new `test_research_null.py`) — tests: shuffle
   preserves the per-date score multiset, leaves `fwds` untouched; same-seed reruns identical,
   different `base_seed` differs; `len(alpha_series) == n_periods` on gappy data; drift universe →
   `|mean(random_IC)| < 0.3` while `alpha_mean > 0.5`. Impl: `_CrossSection`, `_cross_sections`,
   `_shuffled`, `_alpha_series`, `NullSpec`, `_t_stat`; rewire `_ic_series`. Done: all green.
2. **Alpha n_eff + split + embargo** (`ic.py`) — tests: overlapping grid (horizon=20, rebalance=5)
   → `alpha_t` uses `n_eff`, not raw n (mirror `test_t_stat_uses_non_overlapping_n_eff`); split at
   `floor(n·frac)`; embargo drops `stride−1` test samples; tiny segment → both `None`. Impl:
   split/embargo in `evaluate_factor`; ICResult fields `alpha_*`, `n_eff`.
3. **Verdict function** (`ic.py`) — tests: table-driven over `_verdict`: insufficient (n_eff
   floor), reversed (full-sample and OOS sign-flip), noise (each gate leg failing alone, incl.
   raw-IC-pass/alpha-fail — the VT 12/12→1/12 case), confirmed_alive, train_only, split-n/a
   fallback. Impl: `_verdict` + wiring into `evaluate_factor`.
4. **Group equity** (new `groups.py`; new `test_research_groups.py`) — tests: drift universe →
   monotonic totals, `long_short > 0`, `mono_rho == 1.0`; hand-computed 2-period NAV; bucket sizes
   differ ≤1, deterministic under symbol reordering; hold return spans exactly one grid interval
   (mixed-calendar fixture); thin data → `insufficient` + `None` readouts + reason. Done:
   isolation test green (6 modules).
5. **Config + CLI** (`core/config.py`, `cli.py`) — tests: config bounds (reject `train_frac=1.0`,
   `n_groups=1`, `null_seeds=0`); `--train-frac` parse/reject; `--universe` precedence +
   resolution; comment-skip + dedupe in `_research_symbols`. Impl: `_unit_fraction`, flags,
   NullSpec construction. Done: existing CLI validation tests green.
6. **Report render + universe file** (`report.py`, `research/data/sp500.txt`) — tests
   (`test_research_report.py` additions): new columns + verdict render; HLZ note always present;
   survivorship caveat only with `universe_note`; split block only when run; quantile section
   incl. insufficient path; `no data` row unchanged; sp500.txt parses to ≥400 unique symbols with
   `#` headers. Impl: render, `FactorReport` fields, snapshot file. Done: finding-14 tests green.
7. **Gate + packaging** — `ruff` / `mypy --strict` / `pytest`; `hatch build`, confirm `sp500.txt`
   in the wheel; smoke `poseidon research factors --universe sp500 --days 400`; CHANGELOG entry.

## 8. Existing tests at risk (report format changes)

Expected to pass unchanged, but they pin format edges — re-run and respect them:
`test_research_report.py` (rank by |t|; "IC"/"factor"/"thin" substrings); `test_audit_research.py`
(rows start with the factor name; `no data` wording; space-delimited `f" {n_periods} "`;
`ic_by_horizon == {…: None}`; CLI int validation); `test_research_evaluate.py` (new
evaluate_factor args keyword-with-default → back-compatible); `test_research_isolation.py`
(groups.py inside the allowlist). Render changes beyond *adding* columns/sections must update
those assertions in the same task.
