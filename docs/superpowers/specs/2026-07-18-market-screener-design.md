# Poseidon Market Screener — Design Spec

**Goal:** let the autonomous PM trade a broad universe (S&P 500), not just the fixed watchlist. Each
cycle, cheaply **screen** ~500 names (batched daily bars), **rank** them, and hand the AI the **top ~15**
to deep-analyze (classic screen-then-analyze). Off by default; zero behavior change when disabled. The
screener picks *what* to evaluate — never whether to trade; every candidate still flows AI → RiskEngine →
broker unchanged.

## 1. Current state & constraints (grounding)

- `app.py::run_review_cycle` (L1042-1160) feeds `config.all_watchlist_symbols()` into both
  `strategies.scan_all(router, portfolio)` and `agent.run_cycle(watchlist=..., strategy_signals=...)`.
  Single integration seam.
- `StrategyEngine.scan_all` runs each enabled strategy concurrently; strategies bind `self.symbols`
  **at construction** and `scan()` takes **no** universe arg. So it **cannot** screen a supplied list
  today — added as additive `extra_symbols` (§7).
- `DataRouter.bars()` / provider `bars()` are **single-symbol** (`/v2/stocks/{sym}/bars`); no batch
  path. `strategy/base.py::gather_bars` does N concurrent single-symbol calls — 500 of those/screen is
  not throughput-honest. Alpaca **does** support multi-symbol `/v2/stocks/bars?symbols=A,B,C` (crypto
  path already batches); we add a batched method (§4).
- `strategy/indicators.py` + `strategy/base.py::pct_return` are **pure** — imported by both `research/`
  and live strategies; the screener reuses them. **`research/` is severed** (imports only `..core.models`
  + `..strategy.indicators`; imported by no live code). The 501-symbol list is at `research/data/sp500.txt`,
  read only at the CLI edge (`cli.py::_universe_file`, via `importlib.resources`). The **live screener
  must not read into `research/`** — it gets its own copy under `data/` (§3).
- Config: `StrictModel` (extra="forbid"), top-level sub-configs on `AppConfig`, money = `Decimal`.

## 2. Component overview

```
run_review_cycle
  screener.select_candidates()  ─ cached top-15 ─┐    NEW: strategy/screener.py
      └─ router.bars_multi(sp500, "1d", 90)      │    NEW: DataRouter.bars_multi + Alpaca.bars_multi
           └─ load_universe("sp500")             │    NEW: data/universe.py + data/universe/sp500.txt
  symbols = watchlist ∪ candidates ──────────────┤
  scan_all(router, pf, extra_symbols=candidates) │    CHANGED: additive extra_symbols
  agent.run_cycle(watchlist=symbols, ...)  ──────┘    unchanged sig, wider list
  → Decision → OrderManager → RiskEngine → Broker     UNCHANGED — no bypass
```

## 3. Universe file plan

- New package data: `src/poseidon/data/universe/sp500.txt` — a **copy** of `research/data/sp500.txt`
  (same format: one ticker/line, `#` comments, header). Ships automatically as package data (same as
  the existing `research/data/*.txt`; no `pyproject` force-include needed since it lives inside the
  package). Research keeps its own copy — the severance invariant forbids research reading `data/`.
- New pure loader `src/poseidon/data/universe.py`:
  ```python
  from importlib.resources import files
  def load_universe(name: str) -> list[str]:
      """Bundled screener universe → uppercased, de-duped ticker list (order-stable).
      Reads packaged data via importlib.resources so it works from a wheel or a source tree.
      Raises ConfigError for an unknown/empty universe."""
  ```
  Imports only stdlib + `..core.errors`. **No `research` import** (enforced by a test, §11).
- Drift guard test asserts the live copy == the research copy (§11) so the two never diverge silently.

## 4. Batched daily bars (throughput core)

Add a batched bars path; **NotImplementedError-based failover** (mirrors router `_route` L300), no new
`DataCapability`.

- `MarketDataProvider.bars_multi(self, symbols, *, timeframe, limit) -> dict[str, list[Bar]]`
  in `data/base.py` — default `raise NotImplementedError` (like `bars`).
- `AlpacaDataProvider.bars_multi` — `GET /v2/stocks/bars?symbols=A,B,..&timeframe=1Day&start=<lookback>&limit=<n>&feed=iex&adjustment=split&sort=desc`. Chunk symbols (`max_batch_symbols`, default 200/req) and follow `next_page_token` until exhausted; parse each `bars[SYM]` list into chronological `Bar`s (reuse the existing row parser). Missing/failed symbols simply absent from the dict.
- `DataRouter.bars_multi(self, symbols, *, timeframe="1d", limit=90) -> dict[str, list[Bar]]`:
  1. Pick the first available BARS-capable slot; probe `provider.bars_multi(first_chunk)`.
     `NotImplementedError` → try next provider. `ProviderError` → penalize (existing `record_failure`)
     → next provider.
  2. If a provider implements it, run all chunks on that provider; a per-chunk `ProviderError` yields
     an empty result for those symbols (best-effort, never abort).
  3. If **no** provider implements `bars_multi` → **degrade** to a bounded-concurrency `gather_bars`
     over single-symbol `self.bars()` (semaphore, e.g. 16) so the screener still works (just heavier)
     against non-Alpaca stacks.
  4. Apply the same boundary hygiene as `bars()`: drop structurally-unsound bars (`_bar_is_sound`)
     and **drop a symbol whose newest bar is older than `_MAX_BAR_AGE[timeframe]`** (frozen feed →
     can't rank a stale name). No real-time freshness gate — daily bars are historical, and the AI
     re-fetches every candidate through its freshness-gated tools before any order.

## 5. Screener module (`src/poseidon/strategy/screener.py`)

```python
@dataclass(frozen=True)
class ScoredCandidate:
    symbol: str; score: float; dollar_volume: float; r_1m: float; r_3m: float

class MarketScreener:
    def __init__(self, config: ScreenerConfig, router: DataRouter,
                 *, now: Callable[[], float] = time.monotonic) -> None:  # injectable clock for cache tests
        self._config, self._router, self._now = config, router, now
        self._cache: list[str] = []; self._cache_at = 0.0
        self._lock = asyncio.Lock()

    async def select_candidates(self) -> list[str]:
        """Cached top-N ranked symbols; re-screen when the cache TTL lapses.
        NEVER raises — a screen failure returns the last good cache (or []),
        so the caller degrades to the watchlist and the cycle is never blocked."""
        if not self._config.enabled:
            return []
        async with self._lock:  # one screen at a time; concurrent cycles share the result
            if self._cache and self._now() - self._cache_at < self._config.refresh_minutes * 60:
                return list(self._cache)
            try:
                ranked = await self._screen()
            except Exception:   # defensive: screening must never block the cycle
                log.exception("screener failed; reusing last candidates")
                return list(self._cache)
            self._cache, self._cache_at = [c.symbol for c in ranked], self._now()
            return list(self._cache)

    async def _screen(self) -> list[ScoredCandidate]:
        universe = load_universe(self._config.universe)                 # ~500 symbols (pure loader)
        bars_by_symbol = await self._router.bars_multi(universe, timeframe="1d",
                                                       limit=self._config.bars_limit)
        floor = float(self._config.min_dollar_volume)                   # Decimal cfg → float compare
        scored: list[ScoredCandidate] = []
        for symbol, bars in bars_by_symbol.items():
            if len(bars) < 64:                                          # need 63d return + 1
                continue
            closes = [float(b.close) for b in bars]
            adv = _median([closes[i] * bars[i].volume for i in range(len(bars))][-20:])  # 20d ADV$
            if adv < floor:                                             # liquidity floor
                continue
            r1m, r3m = pct_return(closes, 21), pct_return(closes, 63)   # reuse strategy.base helper
            if r1m is None or r3m is None:
                continue
            scored.append(ScoredCandidate(symbol, 0.6 * r1m + 0.4 * r3m, adv, r1m, r3m))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: self._config.top_n]
```

**Ranking = blended momentum** `0.6·r_1m + 0.4·r_3m` behind a **median 20-day dollar-volume floor**.
Cheap (closes + volume, no quotes); reuses pure `pct_return`/`indicators`. Partial data (no/short bars,
below floor) is silently skipped (logged count). Ranking math is `float` (indicator convention, no money
reaches an order); the `Decimal` config threshold is cast at the compare.

## 6. `ScreenerConfig` (core/config.py)

```python
class ScreenerConfig(StrictModel):
    """Market screener: widen the trading universe by pre-screening a broad index each cycle and
    handing the AI the top-N ranked candidates to deep-analyze. Advisory selection only — it picks
    WHAT to evaluate; every candidate still passes the full AI → RiskEngine → broker chain. OFF by
    default: zero behavior change until deliberately enabled."""
    enabled: bool = False
    universe: Literal["sp500"] = "sp500"
    top_n: int = Field(default=15, ge=1, le=100)
    min_dollar_volume: Decimal = Field(default=Decimal("20000000"))   # $20M median 20d ADV floor
    refresh_minutes: int = Field(default=15, ge=1)                    # cache TTL for the ranked list
    bars_limit: int = Field(default=90, ge=64, le=250)               # daily bars/symbol for ranking
    max_batch_symbols: int = Field(default=200, ge=1, le=500)        # symbols per Alpaca batch request
```
On `AppConfig`: `screener: ScreenerConfig = Field(default_factory=ScreenerConfig)`. Add a commented `screener:` block to `config/poseidon.example.yaml` near `guardian:`/`reports:`.

## 7. Integration into `run_review_cycle` + strategy widening

**app.py construction** (near L204 where `StrategyEngine` is built): always construct
`self.screener = MarketScreener(cfg.screener, self.router)` (harmless when disabled — `select_candidates`
early-returns `[]`).

**run_review_cycle** — replace the symbol source at L1066/L1080:
```python
watchlist = self.config.all_watchlist_symbols()
candidates = await self.screener.select_candidates()   # [] when disabled or on failure
symbols = _union(watchlist, candidates)                # watchlist first, order-stable dedup (both upper)
signals = await self.strategies.scan_all(self.router, self.portfolio, extra_symbols=candidates)
...
decision = await self.agent.run_cycle(..., watchlist=symbols, strategy_signals=[s.as_dict() for s in signals], ...)
```
- `candidates == []` (disabled/failed) ⇒ `symbols == watchlist` and `extra_symbols=[]` ⇒ **byte-identical**
  to today. Off-by-default safety holds.
- `_instrument_identities(symbols)` and reflection `relevant_lessons(symbols)` move to the **union**
  (cheap: router-cached identities, DB lessons) so the AI is grounded on candidates. `analysis.relevant_packets`
  stays **watchlist-scoped** (call-heavy; AnalysisConfig is off by default). — open question, §13.

**Strategy widening** — add additive `extra_symbols` (default `None` ⇒ unchanged):
- `StrategyEngine.scan_all(self, router, portfolio, *, extra_symbols=None)` forwards to each
  `strategy.scan(router, portfolio, extra_symbols=extra_symbols)`.
- `Strategy.scan(self, router, portfolio, *, extra_symbols=None)` (abstract sig gains the kwarg, so
  mypy-strict Liskov holds for every override). Base helper
  `self._widen(extra) -> dedup(self.symbols + [s.upper() for s in extra])`.
- Equity screeners (`trend` ×3, `reversion` ×2, `longterm`, `rotation`, `volatility`, `custom`) call
  `self._widen(extra_symbols)` where they currently read `self.symbols`. `options_income` accepts the
  kwarg but **ignores** it (selling options on unheld screened names is out of scope; keeps its narrow
  semantics).
- Because the screener already narrowed 500 → ~15, `scan_all` only ever sees `watchlist ∪ ~15` (≈20-40
  names) — the 500-name work stays isolated in `bars_multi`; strategies keep their cheap `gather_bars`.

## 8. Throughput & cost budget

- Universe 501 symbols × `bars_limit=90` daily bars ≈ 45k bars. Batched at 200 symbols/chunk, following
  Alpaca's 10k-bars/page cap ⇒ **~4-8 HTTP requests per full screen** (vs **501** single-symbol).
- Alpaca free (IEX) ≈ **200 requests/min** — a full screen is ~3% of one minute's budget.
- Cache TTL 15 min ⇒ ~4 screens/hr ⇒ **~24-32 requests/hr** for screening (negligible next to per-cycle
  AI tool calls). At the default 300s cycle cadence the screen refreshes once every ~3 cycles — never
  re-screening 500 names every cycle.
- **Why S&P 500 fits free data but the full universe does not:** 500 large-caps are fully IEX-covered and
  fit in a handful of batched requests. The full ~8k US-equity universe is ~16× the bars, needs the paid
  SIP feed for thin-name coverage, and blows the rate budget — paid data. S&P 500 is the free-tier sweet spot.

## 9. Failure modes (all degrade to the watchlist; never crash the cycle)

1. Alpaca/all batch providers down or penalized → `bars_multi` degrades to bounded single-symbol; if that
   also fails per-symbol those names are absent; if everything fails `_screen` ranks `[]`.
2. `_screen` raises anything → `select_candidates` returns the **last good cache** (or `[]`).
3. Partial data (no/short history, below liquidity floor) → symbol skipped (logged count), rank the rest.
4. 429 mid-screen → provider penalty box (existing) → partial dict → rank what returned.
5. Missing/corrupt universe file → `load_universe` raises → caught by `_screen` → `[]` → watchlist only.
6. Frozen feed / `enabled=False` / empty universe → symbol dropped or `[]` returned → watchlist only.

## 10. Safety checklist

- **No risk bypass.** Candidates flow scan_all → run_cycle → Decision → `OrderManager._process_order`
  → RiskEngine (every rule, incl. `UniverseRule` allow/deny + `VolumeRule` `min_avg_volume`) → Broker.
  Screener touches neither `risk/` nor the order path. **No new order path.**
- **Off by default.** `screener.enabled=False`; disabled path is byte-identical (`candidates=[]`).
- **research/ isolation.** Screener imports `data.universe`, `data.router`, `strategy.base/indicators`
  — never `research`. Own universe copy under `data/`. Test asserts `poseidon.research` is not pulled in.
- **Degrade to watchlist.** Every failure ⇒ `[]`/last cache ⇒ `symbols == watchlist`; cycle proceeds.
- **Money `Decimal`.** `min_dollar_volume` is `Decimal`; no money reaches an order from the screener.
- **Live data.** Screening bars route through `DataRouter` (sound + frozen-feed checked); the AI
  re-fetches candidates through freshness-gated tools before trading.

## 11. Ordered TDD task list (tests first; fake batched provider, no network)

1. **Universe file + loader.** Tests `tests/unit/test_universe.py`:
   `test_load_sp500_501_unique_upper`, `test_skips_comments_and_blanks`, `test_unknown_universe_raises`,
   `test_screener_universe_matches_research_copy` (drift guard),
   `test_load_universe_has_no_research_import` (assert `poseidon.research` absent from the loader module's
   imports). Impl: `data/universe/sp500.txt` (copy), `data/universe.py::load_universe`.
2. **ScreenerConfig.** Tests `tests/unit/test_screener_config.py`: `test_defaults_disabled`,
   `test_top_n_bounds`, `test_min_dollar_volume_is_decimal`, `test_extra_forbidden`. Impl: `ScreenerConfig`
   + `AppConfig.screener` in `core/config.py`.
3. **Batched bars (provider + router).** Add `FakeBatchProvider` to `tests/conftest.py` (implements
   `bars_multi`; a flag makes it raise `NotImplementedError`). Tests `tests/unit/test_bars_multi.py`:
   `test_router_bars_multi_returns_dict`, `test_drops_unsound_bars`, `test_partial_symbols_absent_on_error`,
   `test_degrades_to_single_symbol_when_unimplemented`, `test_frozen_symbol_dropped`,
   `test_alpaca_bars_multi_parses_and_paginates` (stub `_get`, no network). Impl: `MarketDataProvider.bars_multi`
   default, `AlpacaDataProvider.bars_multi`, `DataRouter.bars_multi`.
4. **MarketScreener.** Tests `tests/unit/test_screener.py` (fake router with canned `bars_multi`, injected
   `now`): `test_ranks_by_blended_momentum_top_n`, `test_liquidity_floor_excludes_thin_names`,
   `test_skips_short_history`, `test_caches_within_ttl` (`bars_multi` called once across two calls),
   `test_refreshes_after_ttl`, `test_failure_returns_last_cache`, `test_disabled_returns_empty`.
   Impl: `strategy/screener.py`.
5. **Strategy widening.** Tests in `tests/unit/test_strategies.py`:
   `test_scan_all_extra_symbols_widens_universe`, `test_scan_all_default_unchanged` (byte-identical set),
   `test_custom_algo_sees_extra_symbols`, `test_options_income_ignores_extra_symbols`. Impl:
   `Strategy.scan(..., *, extra_symbols=None)` + `_widen`; `StrategyEngine.scan_all(..., extra_symbols=None)`;
   update builtins + `custom.py`.
6. **Wire the cycle + construct screener.** Tests `tests/integration/test_screener_cycle.py` (FakeBatchProvider
   + PaperBroker + a stub agent capturing `watchlist`): `test_enabled_feeds_candidates_to_watchlist`,
   `test_disabled_watchlist_only` (identical set), `test_screen_failure_degrades_to_watchlist`. Impl:
   construct `self.screener` in `app.py`; union + `extra_symbols` wiring in `run_review_cycle`; `_union` helper.
7. **Config sample + CHANGELOG.** Commented `screener:` block in `config/poseidon.example.yaml` + CHANGELOG entry (not unit-tested).

## 12. Existing tests that may change

- `tests/unit/test_strategies.py` — existing `.scan(router, pf)` calls stay valid (kwarg optional); add new
  cases. Any signature-introspection test would need the new kwarg (none seen).
- `tests/conftest.py` — additive `FakeBatchProvider`; existing `FakeProvider` untouched, so router/risk tests
  are unaffected.
- Config tests — `screener` is additive with a default; a full-`AppConfig` field-set snapshot would need it.
- `tests/integration/test_order_flow.py`, `test_local_backend_cycle.py` — disabled by default ⇒ cycle path
  byte-identical; expected to pass unchanged (verify).

## 13. Open questions / risks

- **Throughput realism (main risk).** Batched multi-symbol bars are the linchpin; if Alpaca's page cap or
  chunk behavior differs from assumed, a full screen could take more requests. Mitigations: chunk size is
  config (`max_batch_symbols`), the 15-min cache amortizes cost, and the single-symbol degrade path always
  works. Validate real request counts against a live key before enabling.
- **scan_all widening cost/benefit.** Threading `extra_symbols` touches ~10 files. Alternative descope: feed
  candidates to `run_cycle(watchlist=...)` **only** (the AI fetches candidate data via its own tools),
  leaving strategies watchlist-scoped — smaller surface, but the AI loses strategy-signal context on
  candidates. Recommend the full widening; flag the descope as the fallback.
- **Advisory scope on the union.** Confirm identities/lessons on the union vs watchlist (cost trade).
- **Static list.** `sp500.txt` is a current-membership snapshot; fine for live screening (we only pick
  names to look at) but drifts on index rebalances — needs periodic refresh.
- **Sector clustering.** A momentum screen can cluster candidates in one hot sector; `RiskEngine`
  `max_sector_concentration_pct` remains the backstop — worth watching in early runs.
```