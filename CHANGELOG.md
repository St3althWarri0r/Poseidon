# Changelog

All notable, user-facing changes to Poseidon. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); releases are also
published as GitHub release notes.

## [2.16.0] — 2026-08-13

Round 3 of the starred-repo cross-pollination. The sources this round —
**OpenBB** (AGPL-3.0) and **awesome-systematic-trading** (unlicensed) — could
not be copied from at all; everything here is implemented from each upstream
data source's own public API, with OpenBB used only as a map of what exists.

**Every new feature is off by default.**

### Fixed — Sharpe and Sortino assumed a zero risk-free rate

- `backtest/stats.py` hardcoded **rf = 0**, so a portfolio merely matching
  T-bills scored a healthy positive Sharpe instead of the honest zero — and the
  PM reads these numbers to choose trades. Both ratios now take
  `risk_free_annual`, and Sortino measures shortfall against the rate rather
  than against zero: a positive day that still loses to cash *is* a shortfall.
- The **live** performance report was fixed alongside the backtest one.
  `analytics/performance.py` had always accepted the parameter; its caller
  passed nothing. The report now also states the rate it used, because a Sharpe
  without its risk-free rate is an unfalsifiable number.
- The bootstrap confidence interval and every walk-forward fold are measured on
  the *same* rate as the headline. Left mixed, a report could print a point
  estimate outside its own confidence interval.
- **Three separate inline copies of the Sharpe formula** were consolidated onto
  `stats.sharpe_ratio`.
- A degenerate-benchmark guard (`sxx <= 0`) was interpreter-dependent: CPython
  3.12+ compensates float summation and yields exact zero for a constant
  series, while earlier versions leave ~1e-35 — and beta then became a division
  *by* that residue, turning rounding noise into a plausible-looking
  coefficient. Now gated on magnitude relative to the series' own scale.

### Added — market state, which the platform never had

- **`data/treasury.py`** — the US Treasury par yield curve, keyless. Serves the
  risk-free rate and the 10Y-3M term spread. (Ken French's `RF` column was
  measured and rejected for the rate: two decimals of a *daily* figure quantizes
  to 2.52%/yr steps — 2023, 2024 and 2025 all report exactly 5.04%.)
- **`data/macro.py`** — VIX plus the curve as one regime snapshot, behind
  `ai.pm_tools.macro_context`. CBOE's feed is delayed and is labelled delayed
  everywhere it appears. The two legs degrade independently: one source down
  still reports the other, both down still returns a snapshot with named gaps
  rather than costing a cycle its decision. A missing leg is a gap, never a zero.

### Added — alpha net of the known factors

- **`backtest/factor_model.py`** with `stats.multi_ols`: Fama-French three-factor
  attribution behind `backtest.factor_attribution`. A strategy holding a size
  tilt is now attributed to SMB instead of scoring "alpha" against a single
  benchmark. Stdlib-only (normal equations via Gauss-Jordan with partial
  pivoting), so collinear regressors are detected rather than silently fitted.

### Added — a screened strategy menu

- **`research/menu.py`** — 51 published systematic strategies with each source
  paper's own reported Sharpe, volatility and cadence, screened against what
  Poseidon can actually trade: 23 runnable, 6 partial, 22 blocked with each
  blocker named. Reported figures are labelled the papers', never Poseidon
  backtests.

## [2.15.0] — 2026-08-13

Research-port wave 2 of the Vibe-Trading / TradingAgents cross-pollination
(ranks 4–7). **Every feature here is off by default** — with no config change
the review cycle behaves exactly as it did in 2.14.0.

### Added — fundamentals, filings and insider data (rank 4)

- Two keyless providers: **SEC EDGAR** (`companyfacts` fundamentals + filing
  metadata, identified User-Agent and polite pacing per SEC fair-access
  policy) and **Yahoo fundamentals**. New `FUNDAMENTALS`, `FILINGS` and
  `INSIDER` data capabilities route through `DataRouter` with reference
  caches, so a provider that cannot serve a class degrades instead of failing
  the cycle.
- Filing metadata only — never document text. Statements are curated to
  10-K/10-Q, USD-family units, newest-first, and carried as `Decimal(str(v))`.
- The PM gains fundamentals tools and a fundamentals analyst context; both are
  config-gated and absent from the catalog when disabled.

### Added — PM research tool breadth (rank 6)

- **`read_url`**: an SSRF-guarded web fetch. Scheme allowlist, no userinfo,
  ports 80/443 only, and no request may reach a private, loopback,
  link-local, multicast, reserved, unspecified or RFC6598 CGNAT address —
  literally, via any resolved A/AAAA record, or behind a redirect (each hop
  is re-validated). Page text is quarantined: scanned for prompt injection
  and annotated, never rewritten, and flagged to the model as data that is
  never a price source.
- **`screen_market`** and an NxN date-aligned **`correlation`** matrix, plus a
  `GET /api/correlation` operator endpoint.

### Added — outcome resolution and behavioural diagnostics (rank 5)

- Decisions carry resolution markers and a lesson `kind`; reflection grades
  the realized `forward_return` against a benchmark and writes
  counterfactual, kind-aware lessons into the cycle prompt.
- `analytics/behavior.py` sweeps closed round-trips for behavioural biases
  (entry-after-runup and friends) — advisory only, never a trade veto.

### Added — backtest evaluation depth (rank 7)

- A stdlib-only stats toolkit: safe CAGR, OLS alpha/beta, bootstrap
  confidence intervals, and randomization significance — **sign-flip for
  Sharpe** (which is order-invariant) and order-permutation for max drawdown.
- Engine summaries gain CAGR, Sortino, Calmar, profit factor and dual
  turnover; rebalance mode gains walk-forward folds, benchmark/OLS
  attribution and a wipeout guard. The strategy workshop reports a backtest
  run card with a hash-pure audit entry.

### Security

- `read_url`'s injection scan now covers the page `<title>`, which reaches the
  model verbatim in its own payload field and was previously unscanned.
- The SSRF guard blocks RFC6598 CGNAT (100.64.0.0/10). Python's `ipaddress`
  reports `is_private`, `is_reserved` and `is_multicast` all false for that
  block, so it evaded every predicate the guard used.
- SEC EDGAR's ticker→CIK cache uses double-checked locking. The check and the
  fill straddled an await, so concurrent cold-start callers each downloaded
  the multi-megabyte ticker map and burst through the fair-access ceiling.

## [2.14.0] — 2026-07-20

### Fixed — manual trading unblocked (#27)

- **Crypto position canonicalization.** Alpaca's positions feed returns crypto
  pairs slashless (`USDTUSD`) while orders, quotes, and risk matching use the
  canonical `USDT/USD` — one real position split across two ledger keys, so
  reduce-only refused its exit as a would-be short and its quote misrouted as
  an equity ticker and failed on every provider. Positions are now
  canonicalized at the broker seam, gated on the broker's own asset class
  (an equity that merely looks like a pair stays raw).
- **Manual delayed-quote carve-out.** The operator's dashboard ticket may
  validate against a DELAYED (≤ `delayed_max_age_seconds`, 15 min) reference
  quote — the after-hours reality on free feeds. STALE always refuses, every
  other risk rule runs unchanged, and the AI cycle, approval re-check, and
  guardian halt-flatten keep the strict live-only gate (each pinned by tests).
  The accepted quote's freshness/source/as_of are recorded in the
  `order.manual_submitted` audit entry.

### Added — per-trade risk case (#26)

- `submit_decision`'s rationale gains a required **invalidation** field: the
  observable condition that proves the thesis wrong, mechanized by the armed
  stop-loss when it is a price. Advisory — missing/malformed values degrade to
  empty and can never void trades.
- **Conviction-scaled sizing** in the PM system prompt: size from the
  volatility-equalized `suggest_position_size` baseline scaled by confidence;
  a high-risk play is licensed exactly when the reward case is a multiple of
  the risk case and the max expected loss stays survivable.
- **Reflection scores conviction**: closed positions carry their entry
  confidence and stated invalidation back to the lesson writer (junk-tolerant
  threading from stored decisions), which judges whether the outcome earned
  the conviction — overconfident losers and underconfident winners both
  become lessons.

### Fixed — actionable local-backend errors (#25)

- HTTP 4xx/5xx from the OpenAI-compatible backend now surface the server's own
  diagnosis (bounded, single-line) in the component-error notification, and
  known context-overflow signatures (LM Studio/llama.cpp/OpenAI/vLLM
  phrasings) append a copy-pasteable remedy naming the configured model.

### Added — release automation

- Pushing a version bump to `main` now auto-tags and publishes the GitHub
  release, using the matching changelog section as the release notes
  (`.github/workflows/release.yml`).

## [2.13.0] — 2026-07-18

### Added — market screener (widen the live trading universe; OFF by default)

The autonomous PM can now trade a broad index (S&P 500), not just the fixed
watchlist. Each review cycle cheaply screens ~500 names with **batched daily
bars**, ranks them by blended momentum behind a dollar-volume floor, and hands
the AI the **top-N** candidates to deep-analyze alongside the watchlist
(classic screen-then-analyze). **OFF by default** (`screener.enabled=false`):
zero behavior change until deliberately enabled — the disabled cycle path is
byte-identical to today.

- **Advisory selection only — no risk bypass.** The screener picks *which*
  symbols the AI evaluates, never whether to trade. Every screened candidate
  still flows through the full AI → RiskEngine → broker chain unchanged (every
  rule, including universe allow/deny and volume floors). No new order path.
- **Degrade to the watchlist, never crash the cycle.** Any screen
  failure/timeout returns the last good cache (or `[]`), so the cycle proceeds
  on the watchlist alone. Partial data (short history, below the liquidity
  floor, frozen feed) is silently skipped and the rest ranked.
- **Batched daily bars** (`DataRouter.bars_multi` + `AlpacaDataProvider`
  multi-symbol `/v2/stocks/bars`): a full screen is ~4–8 HTTP requests instead
  of ~500 single-symbol calls, chunked and page-followed. On a stack without a
  batched provider it degrades to bounded-concurrency single-symbol fetches.
- **Blended-momentum ranking** `0.6·r_1m + 0.4·r_3m` behind a median 20-day
  dollar-volume floor (default $20M), reusing the pure `strategy.indicators`
  helpers; a 15-minute cache amortizes the screen across cycles.
- **`ScreenerConfig`** on `AppConfig` (`screener:`): `enabled`, `universe`
  (`sp500`), `top_n`, `min_dollar_volume` (Decimal), `refresh_minutes`,
  `bars_limit`, `max_batch_symbols` — all bounds-validated. A commented
  `screener:` block is documented in `config/poseidon.example.yaml`.
- **`research/` stays severed.** The live screener ships its own S&P 500 copy
  (`data/universe/sp500.txt`) behind a pure `data.universe.load_universe`
  loader and never imports `research/` (drift-guard + no-import tests).

### Added — factor-bench rigor (`poseidon research factors`)

Offline, deterministic factor diagnostics gain a random-control null gate and
quantile-layering, ported pure-stdlib from Vibe-Trading's strict bench. The
`research/` package stays pure/offline/stdlib-only, seeded-deterministic,
anti-lookahead, sample-stdev (n−1), and non-overlapping-`n_eff`; no live-trading
code imports it.

- **Within-date random-control null.** Per rebalance date, factor scores are
  permuted `null_seeds` times (default 5) with a string-seeded
  `random.Random(f"{base_seed+k}:{date}")` — never wall-clock — and the mean
  random IC is subtracted from the real IC to form a paired `alpha_IC` series.
  `alpha_t` uses the same non-overlapping `n_eff` as the base t-stat (the alpha
  pairs inherit the base overlap).
- **Honest verdicts** on each factor: `insufficient_data`, `reversed`, `noise`,
  `train_only`, `confirmed_alive`. Gate = loose IC/hit/t gate **and** null
  survival (`alpha_t >= alpha_t_threshold`, default 2.0).
- **Optional chronological OOS split** (`--train-frac`, default off) with a
  stride-based embargo so no test window overlaps a train window; too-thin
  segments fall back to full-sample with a labeled "split didn't run" note.
- **Quantile-bucket NAV layering** (`n_groups`, default 5): equal-weight buckets
  by score, hold-to-next-rebalance returns (contiguous, non-overlapping),
  long/short spread and monotonicity ρ; thin data yields a labeled
  `insufficient data (<reason>)` readout, never a confident number.
- **Bundled `--universe sp500`** snapshot (`research/data/sp500.txt`, ~500
  current constituents, dot-class tickers kept) resolved at the CLI edge via
  `importlib.resources`; adds a labeled survivorship caveat to the report.
  Config gains `null_seeds`, `null_base_seed`, `train_frac`, `alpha_t_threshold`,
  `verdict_min_n_eff`, `n_groups` (all bounds-validated).
- Report footer prints the Harvey-Liu-Zhu (2016) note: whole-library scans
  should clear `|t| >= 3.5`; treat `confirmed_alive` below that as provisional.

None-sentinel discipline (finding 14) extends to every new field: never-computed
values render `-`, distinct from a measured `0.0`.
