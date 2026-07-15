# Factor Research Lab (factor library + IC/IR) — Design Spec

**Date:** 2026-07-14
**Status:** Approved (design), pending implementation plan
**Target:** Poseidon v2.11.0 candidate
**Origin:** Sub-project **#4a** of the cross-pollination program ([[poseidon-crosspollination-program]]),
the highest-value borrow from Vibe-Trading's research depth ([[vibe-trading-analysis]]):
a factor/alpha library + IC/IR benchmarking. #4 is decomposed into **4a (this: factors +
IC/IR)** and **4b (strategy-decay tracking, a follow-on that reuses 4a's rolling-IC math)**.

## 1. Goal

Give Poseidon a proper **factor research lab**: a library of systematic alpha factors and
the standard machinery to score each factor's predictive power — the **Information
Coefficient (IC)** and **Information Ratio (IR)** — computed **point-in-time** so the numbers
aren't inflated by look-ahead leakage. Poseidon can backtest a *strategy* (signals → trades →
PnL) but has no way to evaluate a *factor* (does this signal rank future returns, how strongly,
how consistently). This adds exactly that, as an **offline research tool**.

## 2. Invariants

1. **Point-in-time correctness is THE invariant.** A factor value for a symbol at date *t* is
   computed from **only** that symbol's bars with `end.date() <= t`; the factor function never
   receives a future bar. Forward returns (the label) are the **only** future data used, and
   only as the thing being predicted. Leakage silently inflates every IC — so the harness is
   structured so the factor is *handed only the sliced window*, making leakage impossible at
   the factor boundary (verified by test).
2. **Offline research only — zero live-trading surface.** This lives in a new `research/`
   package and **never** touches the risk engine, order path, decision path, `submit_decision`,
   the broker, or the audit chain. It reads historical bars and prints a report. It cannot
   place, size, gate, or influence a live trade in v1 (wiring winning factors into the
   PM/workshop is deliberately a later step).
3. **Pure, dependency-light core.** Factors and the IC harness are pure functions over a
   pre-loaded `history: dict[str, list[Bar]]` (the same shape `BacktestEngine.run` takes) — no
   I/O, no network, fully unit-testable with synthetic bars. Statistics use the **stdlib
   `statistics`** module (`statistics.correlation` on ranks for Spearman); **no numpy/scipy/
   pandas** added.
4. **Money stays Decimal where it's money; stats are float.** `Bar` OHLC are `Money`
   (`Decimal`). Factor math converts close→`float` for statistical computation (IC is a
   statistical measure, not an accounting quantity) — this is explicitly research math, and it
   never feeds an order or a price shown to the trader.

## 3. Design

### 3.1 Package layout — new `src/poseidon/research/`
- `factors.py` — the `Factor` type + the starter factor library.
- `ic.py` — the point-in-time IC/IR harness + the small stats helpers.
- `report.py` — run a factor set over a universe, rank them, render a report.
- `loader.py` — thin async history loader (via `DataRouter`) for the CLI; kept out of the pure core.
- `__init__.py`.
Plus a CLI command `poseidon research factors` (`cli.py`) and a `ResearchConfig` block
(`core/config.py`) for universe/horizons/rebalance defaults.

### 3.2 Factor abstraction (`factors.py`)
```python
@dataclass(frozen=True)
class Factor:
    """A named alpha factor: a pure function from ONE symbol's point-in-time bars to a
    single cross-sectional score (or None if it can't be computed from the given window).
    Higher score = more attractive per the factor's thesis. The harness hands `fn` ONLY the
    bars with end.date() <= as_of, so a factor is structurally unable to see the future."""
    name: str
    fn: Callable[[list[Bar]], float | None]
    description: str = ""
    min_bars: int = 2          # skip a symbol until it has this much visible history
```
Factors are built from the existing `strategy/indicators.py` (sma, rsi, ema, macd, stdev_return,
rate_of_change, atr, obv, …) applied to `[float(b.close) for b in bars]` etc. A small helper
`_closes(bars)/_highs/_lows/_vols` does the `Decimal→float` extraction once.

### 3.3 Starter factor library (~12–15 canonical factors, NOT all 461)
A curated, well-understood set — momentum, reversal, volatility, volume, trend — e.g.
`momentum_12_1` (12-month return skipping the last month), `momentum_6m`,
`short_term_reversal_5d` (negated 5-day return), `low_volatility_20d` (negated stdev of
returns), `rsi_14` (mean-reversion, negated), `price_vs_sma_50` (close/sma−1),
`macd_hist`, `roc_20`, `atr_norm_14` (ATR/close, negated), `max_drawdown_60d` (negated),
`obv_trend_20`, `volume_ratio_20` (recent vs baseline volume). Each is one small pure fn.
`ALL_FACTORS: list[Factor]` is the registry the report iterates. Growing the zoo toward the
461 is future work; the abstraction supports it without change.

### 3.4 IC/IR harness (`ic.py`) — point-in-time
```python
def visible_bars(bars: list[Bar], as_of: date) -> list[Bar]        # end.date() <= as_of
def forward_return(bars: list[Bar], as_of: date, horizon: int) -> float | None
def spearman(xs: list[float], ys: list[float]) -> float | None      # rank then statistics.correlation
def rebalance_dates(history, every: int) -> list[date]              # every Nth common trading day

@dataclass(frozen=True)
class ICResult:
    factor: str
    horizon: int
    ic_mean: float; ic_std: float; ir: float; t_stat: float
    hit_rate: float; n_periods: int
    ic_by_horizon: dict[int, float]    # decay curve: mean IC at horizons 1,5,10,20

def evaluate_factor(factor: Factor, history: dict[str, list[Bar]], *,
                    horizon: int, rebalance_every: int, horizons: list[int],
                    min_cross: int = 5) -> ICResult
```
For each rebalance date *t*: build the cross-section `{sym: (factor.fn(visible_bars(bars,t)),
forward_return(bars,t,horizon))}` over symbols that clear `factor.min_bars` and have a forward
bar; require `>= min_cross` symbols; `IC_t = spearman(values, forwards)`. Over the IC series:
`ic_mean`, `ic_std` (`statistics.stdev` — **sample n−1**, since IR/t-stat is a "differs from
zero" inference; population `pstdev` would nudge every t-stat upward), `ir = ic_mean/ic_std`
(**0.0 when `ic_std == 0`** — no divide-by-zero on a constant IC series),
`hit_rate = fraction(IC_t > 0)`, and `t_stat = ir*sqrt(n_eff)` where **`n_eff` counts only
NON-OVERLAPPING forward windows** — `stride = max(1, ceil(horizon/rebalance_every))`,
`n_eff = ceil(n_periods/stride)`. This is the real "silently overstates the edge" bug: when
`rebalance_every < horizon`, consecutive `IC_t` share forward windows, so the series
autocorrelates and a naïve `sqrt(n_periods)` inflates the t-stat. `rebalance_every` **defaults
to `horizon`** (no overlap); the report warns when it is set smaller. `ic_by_horizon` reruns
the mean-IC for `horizons` to show decay (same non-overlap discipline for any reported t-stat).
`forward_return` finds `i = last index with end.date() <= as_of` and returns
`close[i+horizon]/close[i] − 1` (None if `i+horizon` is out of range) — the factor gets
`bars[:i+1]`, the label uses `bars[i]` and `bars[i+horizon]`, so windows never overlap.

### 3.5 Report (`report.py`)
`run_report(factors, history, cfg) -> FactorReport` evaluates every factor, sorts by
`abs(t_stat)` (or `abs(ir)`), and renders a table: factor · IC mean · IR · t-stat · hit-rate ·
decay. `FactorReport` is a structured dataclass (+ a `render()` text table). Honest labeling:
the report states it is **descriptive point-in-time IC over the supplied history**, warns that
mining many factors on one universe overfits, that IC ≠ tradable PnL (costs/capacity ignored),
and — critically — that **cross-sectional IC quality tracks universe breadth**: ranking across a
handful of names is very noisy and barely meaningful, so the report surfaces the cross-section
size and flags results computed on a thin universe (the analog of "weak on a small model").

### 3.6 CLI (`cli.py`)
`poseidon research factors [--symbols A,B,… | --symbols-file PATH | --watchlist] [--days N]
[--horizon H] [--rebalance-every K]`. The research universe is **independent of the (small)
trading watchlist**: `--symbols`/`--symbols-file` is the primary input and is meant to carry a
**broad** list (hundreds of names) so the cross-sectional IC is meaningful; `--watchlist` is a
convenience shortcut, not the intended universe. `cmd_research` loads history via
`research/loader.py`
(`load_history(router, symbols, days)` → `dict[str, list[Bar]]` using `DataRouter.bars`),
runs `run_report`, prints the table. Loader failures degrade to a clear message; a symbol with
too little history is skipped with a note. The heavy pure work stays testable without the CLI.

### 3.7 Stats
Spearman = rank both vectors (average ranks for ties) then `statistics.correlation(rank_x,
rank_y)` (Pearson on ranks). Guard: return `None` when `n < 3` or a vector is constant
(zero variance → undefined) — pre-check variance and/or catch `statistics.StatisticsError`
→ `None`. `ic_std` via `statistics.stdev` (sample, n−1); `t_stat` uses `math.sqrt` over the
non-overlapping `n_eff` (§3.4), not the raw period count. All stdlib.

## 4. Error handling
Pure functions return `None`/skip rather than raise on thin or degenerate input (too few bars,
constant factor values, out-of-range forward window). The CLI catches loader/`DataError` and
prints a clear message. Nothing here raises into any live path — it is not on one.

## 5. Testing
- **The anti-lookahead invariant (the point):** a probe factor records the max `end.date()` it
  is ever handed; over a multi-date evaluation, assert it is always `<= t`. A factor **cannot**
  observe a future bar. Separately: a deliberately clairvoyant factor (returns the *next* bar's
  return if it could see it) scores IC≈0 because it never receives the future bar.
- **IC correctness on synthetic data (non-circular):** construct symbols whose forward return is
  a deterministic monotone function of a **past-only** signal (e.g. trending series where higher
  trailing momentum ⇒ higher forward return), so a legitimate momentum factor — receiving only
  `bars[:i+1]` — predicts the label and the harness scores IC≈+1; its negation ≈−1; a random/
  constant factor ≈0 with IR/t-stat near 0. The factor must NOT be handed the label — the point
  is to prove the *harness* correlates, not that a factor secretly saw the future.
- **Non-overlapping t-stat:** with `rebalance_every < horizon`, assert `n_eff < n_periods` and
  that `t_stat` uses `n_eff` (a naïve `sqrt(n_periods)` would report a larger, dishonest t-stat).
- `forward_return` and `visible_bars` boundary cases (as_of before first bar, horizon past the
  end, exact-date match). `spearman` vs a hand-computed value and its tie/constant/`n<3` guards.
- Each starter factor computes a finite value on a normal series and `None` on too-short input.
- `run_report` ranks by `|t_stat|` and renders without error; `rebalance_dates` spacing.
- Full gate (ruff / mypy --strict / pytest). No `tools/ui_verify.py` (no UI). No multi-agent
  safety review needed for correctness (it never touches the risk/order path) — but a final
  review should scrutinize **point-in-time leakage** specifically, since that is the one way
  this feature can be silently wrong.

## 6. Scope / YAGNI
- **~12–15 starter factors, not 461.** The abstraction scales; validating hundreds is its own
  effort. Start with the canonical set.
- **No numpy/scipy/pandas** — stdlib stats keep the dependency surface flat.
- **Not wired into live trading in v1.** No factor output reaches the PM, the workshop, or an
  order. Surfacing top factors as advisory context is a later, separately-designed step.
- **No factor *optimization*/fitting.** Factors are parameter-fixed; the lab *measures*, it does
  not search — which also keeps the IC honest (no in-sample parameter mining).
- **Cross-sectional IC only** (rank factor vs forward return across the universe); time-series/
  quantile-portfolio backtests of factors are future work (the existing `BacktestEngine` already
  covers strategy-level PnL).

## 7. Sequencing
Branch `feat/factor-research-lab` off current `main` (v2.10.0). Its own release **v2.11.0**
(this feature is independent of the tiering/packet branches, which are already merged). **4b
(strategy-decay tracking)** follows as its own spec, reusing `ic.py`'s rolling-IC machinery to
drive a candidate/active/decaying/retired lifecycle over *live* strategy performance.
