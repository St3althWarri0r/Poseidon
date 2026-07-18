# Changelog

All notable, user-facing changes to Poseidon. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); releases are also
published as GitHub release notes.

## [Unreleased] — 2.13.0 candidate

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
