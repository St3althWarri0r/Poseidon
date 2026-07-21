# Changelog

All notable, user-facing changes to Poseidon. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); releases are also
published as GitHub release notes.

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
