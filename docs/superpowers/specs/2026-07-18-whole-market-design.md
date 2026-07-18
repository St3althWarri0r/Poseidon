# Design spec — Trade the whole market via screening (no fixed watchlist)

Status: proposed · Date: 2026-07-18 · Target: Poseidon `main` (post S&P-500 screener merge)
PM model: local gpt-oss-20b (32k context, OpenAI-compatible backend)

## Goal

Retire the hand-maintained watchlist. Each review cycle the PM analyzes candidates
chosen by two screeners: the S&P-500 **equity** screener (already merged, market-hours
only) and a new **crypto** screener over ~40 liquid Coinbase USD pairs (24/7). The PM
deep-analyzes the top-N of each. The fixed watchlist becomes optional (default empty).

## Non-goals / invariants (preserve)

- No risk bypass: screeners pick only WHICH symbols the PM sees; every candidate still
  flows PM → RiskEngine → Broker unchanged (one order path, `OrderManager._process_order`).
- Money is `Decimal`; ranking math stays `float` (indicator convention) as today.
- `research/` untouched; the screener keeps its severed `data.universe` copy.
- Every screener/prompt-bound failure degrades gracefully — a cycle NEVER crashes.
- Live-data-only: crypto bars flow through `DataRouter` (staleness + OHLC hygiene).
- The token bound must NOT silently starve the PM of data for symbols it is deciding on.

## Build order (STRICT)

1. **[FOUNDATION] Token-bounded cycle prompt** — prereq; without it 15 equity + 10
   crypto candidates overflow gpt-oss worse than the ~10 that already hit HTTP 400
   `exceed_context_size_error`. Ship + verify FIRST, independent of the screener.
2. **[FEATURE] Crypto screener** — generalize `MarketScreener`; add `crypto.txt`.
3. **[WIRING] Dual-screener, no watchlist** — union both screeners into the cycle.

---

## Part 1 — [FOUNDATION] Token-bounded cycle prompt

### Problem (measured)

The PM's per-cycle input is unbounded on three axes; any one can blow a 32k window,
and they accumulate across the tool loop (`agent.run_cycle`, messages list grows):

- **(a) `strategy_signals`** — `_cycle_prompt` does `json.dumps(strategy_signals)` with
  NO cap (`agent.py:202`). `Signal.evidence` is an arbitrary dict; widening the universe
  multiplies signal count. Unbounded.
- **(b) tool results** — `ToolDispatcher._MAX_RESULT_CHARS = 60_000` (`tools.py:27`).
  60k chars ≈ 15k tokens: ONE `get_bars(limit=500)` or `get_news(limit=50)` result can
  half-fill the window. Results persist in `messages` for the rest of the cycle.
- **(c) no per-cycle ceiling** — nothing bounds prompt + accumulated tool output overall.

### Fix — layered budget (primary = per-tool + prompt caps; backstop = cycle ceiling)

New config `CycleBudgetConfig` (StrictModel) under `AIConfig` as `ai.budget`, all fields
tuned so a 25-candidate cycle sits well under ~20k tokens in a 32k window:

| field | default | bounds what |
|---|---|---|
| `max_signal_entries` | 40 | top-K signals kept in the prompt (by `strength` desc) |
| `max_signals_chars` | 8000 | hard cap on the serialized signals block |
| `max_prompt_chars` | 16000 | assembled user-turn budget (backstop truncation) |
| `max_bars_returned` | 120 | `get_bars`: bars handed to the model |
| `max_news_articles` | 10 | `get_news`: article count |
| `max_news_summary_chars` | 500 | `get_news`: per-article summary length |
| `max_tool_result_chars` | 12000 | per-result hard cap (replaces the 60_000 constant) |
| `soft_cycle_tool_chars` | 40000 | cumulative tool output → append a "converge" nudge |
| `hard_cycle_tool_chars` | 64000 | cumulative last-resort backstop (~16k tok) |

**(a) Signals — `agent.py::_cycle_prompt`.** Add a helper `_bounded_signals(signals, cfg)`:
sort by `strength` desc, keep `max_signal_entries`, render each compactly (symbol,
direction, strength, and only scalar/short evidence values — drop nested/long evidence),
`json.dumps` the kept list, then hard-truncate to `max_signals_chars` on a whole-entry
boundary. Append `"(… N lower-strength signals omitted)"` so the omission is explicit,
never silent. Highest-conviction signals survive — the ones most likely to become trades.

**(b) Per-tool caps — `tools.py`.** Thread `budget: CycleBudgetConfig` into `ToolDispatcher`.
- `_tool_get_bars`: return at most `max_bars_returned` bars (keep the NEWEST); if the
  requested `limit` exceeded it, add `"note": "series capped to the most recent N bars"`
  so the model knows it was capped, not that data is missing.
- `_tool_get_news`: slice to `max_news_articles`; truncate each `summary` to
  `max_news_summary_chars` with an ellipsis marker.
- `get_market_snapshot`: already bounded (20 closes + fixed indicator set, ~1.5–2.5k
  chars) — leave the payload shape; it is the anti-confabulation surface. Do NOT shrink it.
- `dispatch`: cap each result at `max_tool_result_chars` (was 60_000). KEEP the existing
  `_truncate` envelope semantics — never a mid-token slice of a price; bound COUNT, never
  cut a number. A cut `412.87 → 412.8` is a plausible-but-wrong quote; the envelope stays.

**(c) Per-cycle ceiling — `tools.py` (stateful, reset per cycle).** Add
`self._cycle_tool_chars = 0` and `reset_cycle_budget()`; `run_cycle` calls it alongside
`sources_used.clear()`. In `dispatch`, after serializing, add the length to the counter:
- over `soft_cycle_tool_chars`: prepend a one-line note to the result — "substantial data
  gathered (~Xk chars); prefer the candidate summaries you already have and converge to
  submit_decision" — but STILL return the real data.
- over `hard_cycle_tool_chars`: data tools return a compact envelope ("per-cycle data
  budget reached; decide with what you have or record a data_gap"). Set high enough that a
  well-behaved cycle never reaches it — a backstop against a runaway loop, not a normal path.

**Anti-starvation guarantee.** The candidate ranked block (Part 3) carries every
candidate's screener metrics inline and is ALWAYS fully included (never dropped by any
cap). So even in the worst case the PM sees each candidate's screen rationale; the caps
only bound how much *extra* per-symbol raw data accumulates.

### Budget math (25 candidates)

system (cached ~1.2k) + tool schemas (~1.5k) + user turn (≤16k chars ≈ 4k) +
tool loop (top ~15 snapshots ≈ 6k + a few capped get_bars/news ≈ 4k) + model output
≈ **well under 20k tokens** — comfortable in 32k.

### Files
`core/config.py` (+`CycleBudgetConfig`, `AIConfig.budget`), `ai/agent.py`
(`_cycle_prompt` signal bounding + `reset_cycle_budget()` call), `ai/tools.py`
(dispatcher budget threading, per-tool caps, cycle counter).

---

## Part 2 — [FEATURE] Crypto screener

### Universe file `data/universe/crypto.txt`

~40 canonical `BASE/USD` Coinbase pairs, one per line, `#` header (mirrors `sp500.txt`)
documenting: curated snapshot, needs periodic refresh, Coinbase-listed USD spot only.
`load_universe` already upcases + de-dupes and preserves `/` — **no loader change**.

```
# Bundled screener universe: top liquid Coinbase USD spot pairs (curated snapshot).
# source: Coinbase Exchange product list, ranked by USD volume. as_of: 2026-07
# WARNING: curated point-in-time snapshot — refresh as listings/liquidity change.
BTC/USD ETH/USD SOL/USD XRP/USD DOGE/USD ADA/USD AVAX/USD LINK/USD DOT/USD LTC/USD
BCH/USD XLM/USD UNI/USD AAVE/USD ATOM/USD ETC/USD FIL/USD APT/USD ARB/USD OP/USD
NEAR/USD ICP/USD INJ/USD SUI/USD LDO/USD MKR/USD RNDR/USD GRT/USD ALGO/USD SAND/USD
MANA/USD AXS/USD SHIB/USD CRV/USD SNX/USD COMP/USD FET/USD DYDX/USD IMX/USD APE/USD
```
(one per line in the file; shown packed here). ~40 is the sweet spot — beyond ~50 the
tail gets illiquid. Universe size is bounded by data throughput + liquidity, NOT the
model: the screener only hands the PM the TOP-N.

### Generalize `MarketScreener` (`strategy/screener.py`)

Currently equity-only via `router.bars_multi(universe, "1d", bars_limit)`. Two things
differ for crypto: routing must be gated to CRYPTO providers, and Coinbase is per-symbol.
Both are absorbed by parameters — the ranking (blended `0.6·r1m + 0.4·r3m` behind a median
20d ADV$ floor), the TTL cache, the `asyncio.Lock`, and degrade-to-last-good/`[]` are
IDENTICAL, so REUSE not duplicate.

- Split `ScreenerConfig` common fields into a base `ScreenerConfigBase(StrictModel)`:
  `enabled, top_n, min_dollar_volume, refresh_minutes, bars_limit`. Equity subclass adds
  `universe: Literal["sp500"]` + `max_batch_symbols`; crypto subclass (below) adds
  `universe: Literal["crypto"]` + `concurrency`. `MarketScreener` types against the base.
- Add ctor kwargs `require: DataCapability | None = None`, `concurrency: int | None = None`
  (stored, passed through). `_screen` calls
  `router.bars_multi(universe, timeframe="1d", limit=cfg.bars_limit, require=self._require,
  concurrency=self._concurrency)`. Equity passes `require=None, concurrency=None` ⇒
  byte-identical to today. Crypto passes `require=CRYPTO, concurrency=cfg.concurrency`.

### Router change (`data/router.py::bars_multi`) — mirrors existing `_route(require=…)`

Add `require: DataCapability | None = None` and `concurrency: int | None = None`.
```python
capable = [s for s in self._slots
           if DataCapability.BARS in s.provider.capabilities()
           and (require is None or require in s.provider.capabilities())]
```
Thread `concurrency` into `_bars_multi_via_single` (its `Semaphore(concurrency or 16)`).
Why required: Alpaca's `bars_multi` filters out crypto symbols (`alpaca_data.py:172`,
`equities = [... if not is_crypto_symbol]`) and returns `{}` SUCCESSFULLY — so an ungated
crypto `bars_multi` silently yields nothing. Gating to `require=CRYPTO` makes Coinbase the
only capable provider; Coinbase has no batch endpoint → `NotImplementedError` → router
degrades to bounded single-symbol `bars()` (each self-routes to Coinbase via
`is_crypto_symbol`). `bars()` already applies OHLC hygiene + frozen-feed rejection, so
crypto bars are sanitized identically to equity.

### Crypto config (`core/config.py`)

```python
class CryptoScreenerConfig(ScreenerConfigBase):
    enabled: bool = True
    universe: Literal["crypto"] = "crypto"
    top_n: int = Field(default=10, ge=1, le=100)
    min_dollar_volume: Decimal = Field(default=Decimal("10000000"))  # $10M median 20d ADV$
    refresh_minutes: int = Field(default=15, ge=1)
    bars_limit: int = Field(default=90, ge=64, le=250)  # Coinbase daily candles
    concurrency: int = Field(default=6, ge=1, le=20)    # bounded Coinbase fan-out
```
`concurrency=6` keeps ~40 per-symbol Coinbase fetches inside its public rate limit
(~10 req/s/IP). `AppConfig` gains `crypto_screener: CryptoScreenerConfig`.

### Files
`data/universe/crypto.txt` (new), `strategy/screener.py` (base-typed config, `require`/
`concurrency` kwargs), `data/router.py` (`bars_multi` `require`+`concurrency`),
`core/config.py` (`ScreenerConfigBase`, `CryptoScreenerConfig`).

---

## Part 3 — [WIRING] Dual-screener, no watchlist

### `app.py`

- `__init__`/`start`: build a second screener
  `self.crypto_screener = MarketScreener(cfg.crypto_screener, self.router,
  require=DataCapability.CRYPTO, concurrency=cfg.crypto_screener.concurrency)`.
- `run_review_cycle` (~line 1103), replace the single-screener block:
```python
watchlist = self.config.all_watchlist_symbols()           # default [] now
market_open = self.clock.session() is not MarketSession.CLOSED
equity = await self.screener.select_candidates() if market_open else []  # equity: hours only
crypto = await self.crypto_screener.select_candidates()   # crypto: 24/7, always
candidates = _dedup(equity + crypto)                      # order-stable, case-insensitive
symbols = _union(watchlist, candidates)
signals = await self.strategies.scan_all(self.router, self.portfolio, extra_symbols=candidates)
```
`_union` already returns `watchlist` unchanged when `candidates == []`, and an empty
`symbols` is a valid cycle (PM reviews portfolio/risk only — `_cycle_prompt` renders
`Watchlist: (empty)`). Lessons/identities already follow `symbols`; keep packets
watchlist-scoped. Reuse the existing `_union`; add a tiny `_dedup(list)` helper (or fold
both screener lists through `_union(equity, crypto)`).
- Pass the ranked candidate block: extend `select_candidates` to optionally expose the
  `ScoredCandidate` metrics (or a parallel `ranked_candidates()`), and hand
  `agent.run_cycle` a compact per-candidate line (`SYM score=+0.12 r1m=+0.08 r3m=+0.19
  adv$=…`) rendered into the prompt — bounded (~70 chars × ~25) and ALWAYS included.

### Config default flip (the point of the feature)

`ScreenerConfig.enabled` default `False → True`; `crypto_screener.enabled` default `True`;
watchlists default empty. Even ON, both degrade to `[]` (disabled, screen failure, OR no
capable provider configured → `bars_multi` `capable == []` → `select_candidates() == []`).
Trading mode default stays `RESEARCH`, so nothing trades regardless of screener state.

### Files
`app.py` (dual construction + `run_review_cycle` union + candidate block), `core/config.py`
(default flips).

---

## Failure modes (all degrade, never crash)

| condition | behavior |
|---|---|
| crypto screen fails / Coinbase down | `select_candidates` returns last-good cache or `[]` |
| no CRYPTO provider configured | `bars_multi` capable set empty → `{}` → `[]` |
| Coinbase 429 on some symbols | those symbols absent this cycle (per-symbol `DataError` swallowed); TTL cache limits screens to ~4/hr |
| equity market closed | equity candidates `[]`; crypto still contributes |
| both screeners `[]` + empty watchlist | valid portfolio-only cycle (empty `symbols`) |
| signals/tool output huge | bounded by Part 1 caps; PM still sees candidate ranked block |
| prompt still near limit | `max_prompt_chars` backstop truncates on entry boundary w/ marker |

## Safety checklist

- [ ] Screened candidates flow PM → RiskEngine → Broker unchanged (no new order path).
- [ ] Money `Decimal`; ranking `float`; `min_dollar_volume` cast to float only at compare.
- [ ] `research/` not imported; screener uses its own `data.universe`.
- [ ] `select_candidates` never raises (both screeners); `bars_multi` never raises.
- [ ] Token caps bound COUNT, never truncate a price mid-token; snapshot payload intact.
- [ ] Candidate ranked block always fully present — PM never blind to a candidate.
- [ ] `run_review_cycle` never crashes on empty `symbols`.
- [ ] Crypto routing gated to CRYPTO providers (no equity provider serves a `/USD` pair).
- [ ] Coinbase concurrency bounded (default 6); TTL cache throttles screen frequency.

## Ordered TDD task list (tests first; fakes only, NO network)

1. **Prompt/signal bounding.** Tests (`test_cycle_prompt_budget.py`): 200 fat-evidence
   signals → serialized block ≤ `max_signals_chars`, only top-K by strength kept, omission
   marker present, highest-strength survives; empty signals → `"none"` unchanged.
   Impl: `CycleBudgetConfig`; `_bounded_signals` in `agent.py`. Files: `core/config.py`,
   `ai/agent.py`.
2. **Tool-result bounding.** Tests (`test_tool_budget.py`, fake router): `get_bars`
   capped to `max_bars_returned` w/ note; `get_news` capped + summary truncated;
   per-result ≤ `max_tool_result_chars`; cumulative > soft → nudge, > hard → envelope;
   `reset_cycle_budget()` zeroes the counter. Impl: dispatcher budget + counter.
   Files: `ai/tools.py`, `ai/agent.py` (reset call).
3. **Router `bars_multi` require + concurrency.** Tests (`test_bars_multi.py`, extend):
   crypto symbols with an equity-only batch provider present → routes to CRYPTO provider
   only (or `{}` if none); `concurrency` bounds the degrade fan-out; equity path
   (`require=None`) byte-identical. Impl: `require`/`concurrency` params. File:
   `data/router.py`.
4. **Crypto universe.** Tests (`test_universe.py`, extend): `load_universe("crypto")`
   returns upcased `BASE/USD` list, de-duped, `/` preserved, every entry
   `is_crypto_symbol`. Impl: add `crypto.txt`. File: `data/universe/crypto.txt`.
5. **Crypto screener + config.** Tests (`test_crypto_screener.py`, `test_screener_config.py`
   extend): `MarketScreener(CryptoScreenerConfig, fake, require=CRYPTO)` ranks BASE/USD by
   blended momentum behind the $-vol floor; passes `require`/`concurrency` to `bars_multi`;
   TTL cache + degrade-to-`[]` reused; `CryptoScreenerConfig` defaults asserted. Impl:
   `ScreenerConfigBase`, `CryptoScreenerConfig`, screener kwargs. Files: `core/config.py`,
   `strategy/screener.py`.
6. **Dual wiring.** Tests (`test_screener_cycle.py`, extend / `test_dual_screener.py`):
   fake equity+crypto screeners → `symbols` is their union; equity gated on market hours,
   crypto unconditional; both `[]` + empty watchlist → cycle completes no-crash; candidates
   still flow to RiskEngine. Impl: `run_review_cycle` union + candidate block + default
   flips. Files: `app.py`, `core/config.py`.

## Existing tests that WILL change

- `test_screener_config.py::test_defaults_disabled` — asserts `enabled is False`; the
  default flips to `True`. Update to assert the new default (and cover `CryptoScreenerConfig`).
- `test_screener.py::FakeRouter.bars_multi` — signature must accept `require=`/`concurrency=`
  kwargs (add `**_`), since the screener now passes them.
- `test_bars_multi.py` — existing calls stay green (defaulted params); add crypto-routing +
  concurrency cases.
- `tests/integration/test_screener_cycle.py` — asserts single-screener/watchlist behavior;
  update for dual-screener + default-on. Any cycle test asserting a byte-identical prompt
  may shift (signals now bounded, candidate block added) — assert bounds, not exact text.
- `test_tool_market_snapshot.py` — `ToolDispatcher(...)` gains an optional `budget=` arg
  (defaulted → stays green; add a budget-path case).
- `_cycle_prompt` callers (`test_agent_backend.py`, `test_identity_injection.py`,
  `test_lesson_injection.py`, `test_local_model_robustness.py`, …) — new prompt params are
  optional/defaulted, so they stay green; add explicit budget assertions where relevant.

## Open risks

- **Coinbase throughput** — ~40 per-symbol daily-candle fetches per screen. At
  `concurrency=6` and a 15-min TTL (~4 screens/hr) this stays inside the ~10 req/s public
  limit; a burst still only costs absent symbols that cycle (never a crash). If refresh is
  shortened or the universe grows past ~50, revisit concurrency / add a small inter-request
  delay. This is the primary throughput risk to watch in production.
- **Budget defaults are estimates** — validate the ~20k-token target against a real
  25-candidate gpt-oss cycle (a token-count assertion in an integration test) and tune the
  `CycleBudgetConfig` defaults before flipping screeners on by default.
