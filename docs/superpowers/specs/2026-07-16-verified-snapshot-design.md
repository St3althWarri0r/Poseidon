# Verified Market Snapshot & Instrument Grounding — Design Spec

**Date:** 2026-07-16
**Status:** Approved (design), pending implementation plan
**Target:** Poseidon v2.13.0 candidate (cross-pollination round 2, wave 1 — program rank 1)
**Origin:** `~/Desktop/Claude/poseidon-crosspoll-r2/program.md` §1; enriches the round-1
debate-packet snapshot (2026-07-14-debate-packet-design.md §3.3).

## 1. Goal

Kill the cheapest confabulation classes at zero LLM cost: (a) analysts and the PM reason over
a price-only snapshot and pad it with recalled/invented numbers; (b) nothing tells any model
*which company* a ticker is, so a weak model can analyze the wrong instrument. Fix: enrich
`build_snapshot` into a deterministic ground-truth block, expose it as a `get_market_snapshot`
PM/chat tool, and inject identity + a do-not-substitute rule into analyst snapshots and
`_cycle_prompt`.

**Found while reading the code (this project also fixes it):** `snapshot.py:42` reads
`q.price`, but `core.models.Quote` has no `price` field — on the real `DataRouter` the render
raises `AttributeError`, `analyze_symbol` swallows it, and **no analysis packet has ever been
built in production** (only test fakes define `.price`). Fixed via `q.last` + regression test.

## 2. Non-goals

- No LLM calls; enrichment is pure computation on router data. Analysts stay tool-less — the
  snapshot is computed FOR them; the fan-out shape in `analysts.py` is untouched.
- No fundamentals/filings/insider data (rank 4), no calc/cross-validate tools (rank 8), no new
  indicator math — only compositions of existing `strategy/indicators.py` functions.
- No DB changes; the profile cache is in-memory in `DataRouter` like the sector cache, and
  `sector()` stays as-is. No change to `RiskEngine`, `OrderManager`, `submit_decision`, or
  any execution path.

## 3. Design

### 3.1 Instrument identity — `data/` layer

**`core/models.py`** — new reference-data model (no Money fields):
```python
class InstrumentProfile(PoseidonModel):
    symbol: str                    # upper-cased, like Quote
    name: str                      # "Apple Inc"
    exchange: str | None = None    # "NASDAQ NMS - GLOBAL MARKET"
    currency: str | None = None    # "USD"
    asset_type: str = "equity"     # honesty note below
    as_of: datetime
    source: str
```
**`data/base.py`** — `DataCapability.PROFILE = "profile"`; base method
`async def profile(self, symbol: str) -> InstrumentProfile: raise NotImplementedError`.

**`data/providers/finnhub.py`** — `capabilities()` += `PROFILE`;
```python
async def profile(self, symbol: str) -> InstrumentProfile:
    payload = await self._get("/stock/profile2", symbol=symbol.upper())
    name = (payload or {}).get("name")
    if not name:
        raise ProviderError(self.name, f"no company profile for {symbol}", retryable=False)
    return InstrumentProfile(symbol=symbol, name=str(name),
                             exchange=(payload.get("exchange") or None),
                             currency=(payload.get("currency") or None),
                             asset_type="equity", as_of=self._now(), source=self.name)
```
*Honesty note:* profile2 has no security-type field and only resolves listed companies
(ETFs/crypto/indices return `{}`), so `asset_type="equity"` when resolved is a fact; anything
else fails open to ticker-only (§4). Free tier; the cache means ~one call/symbol/week.

**`data/router.py`** — `async def profile(self, symbol: str) -> InstrumentProfile | None`,
body shaped exactly like `sector()`: upper-case; `_profile_cache: dict[str,
tuple[InstrumentProfile | None, float]]` with `_PROFILE_TTL = 7 * 86400.0` /
`_PROFILE_NEGATIVE_TTL = 3600.0`; route `DataCapability.PROFILE`; catch
`(AllProvidersFailedError, DataUnavailableError)` → cache `(None, now)` → return `None`
(*unresolved*, never a guess).

### 3.2 Config — `SnapshotConfig` (`core/config.py`), nested as `ai.snapshot`
```python
class SnapshotConfig(StrictModel):
    """Deterministic snapshot enrichment + identity grounding (advisory text only).
    ON by default — deliberate exception to ship-OFF: zero LLM cost, enriches existing
    prompt surfaces (no new AI surface), every failure degrades to explicit
    N/A/unresolved, and it only REMOVES hallucination room. The one new tool is
    deterministic, read-only, on the same router as get_quote/get_bars."""
    bars_limit: int = Field(default=250, ge=50, le=500)  # daily bars (SMA200 needs 200)
    closes_n: int = Field(default=20, ge=5, le=120)      # last-N closes listed verbatim
    identity: bool = True                                # resolve + inject identity
```
`AIConfig` gains `snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)` (keys `ai.snapshot.*`).

### 3.3 Enriched snapshot — `ai/analysis/snapshot.py`
```python
@dataclass(frozen=True)
class Snapshot:
    symbol: str
    as_of: datetime                        # quote as_of (unchanged)
    source: str                            # quote source (unchanged)
    text: str                              # block analysts cite verbatim (unchanged role)
    sources: tuple[str, ...] = ()          # every live source consulted (quote/bars/profile)
    payload: dict[str, Any] | None = None  # structured tool JSON — exact strings only

async def build_snapshot(router: object, symbol: str, *, config: SnapshotConfig | None = None,
                         allow_delayed: bool = True) -> Snapshot | None:
```
New fields are trailing defaults, so existing positional constructions keep working.
Build order, each part in its **own** try/except (today one blanket try kills everything):
1. **Quote (mandatory):** `router.quote(symbol, allow_delayed=allow_delayed)`; failure →
   `None` (analyst path skips; tool path raises, §3.4). Price is
   `q.last if q.last is not None else q.mid` — the `q.price` bug fix.
2. **Bars (degrade):** `router.bars(symbol, timeframe="1d", limit=cfg.bars_limit)`; failure/
   empty → OHLCV `null`, closes `[]`, every indicator `"N/A (bars unavailable)"`.
3. **Profile (degrade):** `router.profile(symbol)` when `cfg.identity`, wrapped so a
   router without `.profile` also fails open → ticker-only identity line.

**Fixed indicator tuple** — from `closes_f = [float(b.close) for b in bars]` (+ highs/lows
for ATR), exactly these existing functions, this order: `sma(closes_f, 50)`, `sma(closes_f,
200)`, `ema(closes_f, 10)`, `macd(closes_f)` (12,26,9), `rsi(closes_f, 14)`,
`bollinger(closes_f, 20, 2.0)`, `atr(highs_f, lows_f, closes_f, 14)`. Each returns `None` on
short history — rendered `"N/A (insufficient history)"`, **never estimated**.

**Exactness (round 1 caught a lossy `{price:.2f}` here):** every Money value — last, OHLC,
closes, 30d range — renders via `str(Decimal)`: no format spec, no `float()` on the display
path. Floats exist only as indicator *inputs*; outputs render `f"{v:.4f}"`, labeled derived.

**`text` template** (identity first; `payload` mirrors it structurally):
```
AAPL pinned live snapshot (cite these exact numbers; do not invent others):
identity: Apple Inc — exchange NASDAQ NMS - GLOBAL MARKET, type equity, currency USD (profile as_of 2026-07-16T09:00:00+00:00, source finnhub)
last 190.10 (quote as_of 2026-07-16T15:30:02+00:00, source alpaca, freshness real_time)
latest daily bar 2026-07-15: O 189.20 H 191.05 L 188.90 C 190.55 V 51234567 (source alpaca)
30d close range 182.11-195.40
last 20 closes (oldest first): 183.15, 184.02, ..., 190.55
indicators (derived from the daily closes above; N/A = unavailable, never estimated): SMA50 187.4321; SMA200 179.8812; EMA10 189.7741; MACD(12,26,9) line 1.2311 signal 0.9902 hist 0.2409; RSI14 61.3200; Bollinger(20,2) upper 194.1100 mid 188.0200 lower 181.9300 %B 0.7100; ATR14 3.4412
Rules: this snapshot is the source of truth for exact numbers — if any other source disagrees, flag the discrepancy; never average or reconcile. Analyze ONLY the instrument identified above; if the name/exchange conflicts with what you expected for this ticker, say so and do not substitute a different company.
```
Unresolved variant of line 2: `identity: unresolved — ticker AAPL only (no live profile); do not infer the company from memory.`

`payload` keys (prices exact strings; indicators strings or `"N/A"`): `symbol`, `identity`
(`{name, exchange, asset_type, currency, as_of, source}` or `{resolved: false, note}`),
`quote` (`{last, as_of, source, freshness}`), `latest_bar` (or `null`), `closes` (`{n,
oldest_first: true, values}`), `range_30d` (or `null`, from `closes[-30:]`), `indicators`,
`as_of`, `sources`, and `note` verbatim: `"Source of truth for exact numbers this cycle. If
another tool result, news text, or recalled figure disagrees, flag the discrepancy in your
rationale/data_gaps — never average or reconcile numbers yourself."` Analysts inherit all of
it through `snapshot.text`; `run_analysts` and `assemble`/`snapshot_digest` are unchanged.

### 3.4 `get_market_snapshot` PM/chat tool — `ai/schemas.py` + `ai/tools.py`
Appended to `DATA_TOOLS` (PM cycle **and** chat; read-only data — chat still cannot trade):
```python
_simple_tool(
    "get_market_snapshot",
    "Verified deterministic snapshot for a symbol: resolved instrument identity, live "
    "quote, latest daily OHLCV bar, last-N closes, and a fixed indicator set (SMA50/200, "
    "EMA10, MACD, RSI14, Bollinger, ATR14) — every number computed platform-side from live "
    "provider data, never by a model. This snapshot is the source of truth for exact "
    "numbers: if any other tool result, news text, or recalled figure disagrees, flag the "
    "discrepancy — never reconcile. N/A values are unavailable; never derive or estimate.",
    {"symbol": {"type": "string"}}, ["symbol"],
)
```
`ToolDispatcher.__init__` gains `snapshot_config: SnapshotConfig | None = None`
(`self._snapshot_config = snapshot_config or SnapshotConfig()`); handler:
```python
async def _tool_get_market_snapshot(self, symbol: str) -> dict[str, Any]:
    snap = await build_snapshot(self._router, symbol, config=self._snapshot_config,
                                allow_delayed=self._allow_delayed)
    if snap is None or snap.payload is None:
        raise DataError(f"no live snapshot available for {symbol}")
    self.sources_used.update(snap.sources)   # provenance → Decision.data_sources
    return snap.payload
```
`DataError` rides the existing envelope ("do not estimate … record in data_gaps"); the payload (≤120 closes ≈ 2–3 KB) sits far under the 60 KB truncation bound.

### 3.5 Identity injection into `_cycle_prompt` — placement vs the prompt cache
The Anthropic backend cache-controls the **system** block (`anthropic_backend.py:43-44`);
tools+system form the frozen cached prefix, so **identity data goes only in the per-cycle
user turn**. `SYSTEM_PROMPT`/`CHAT_SYSTEM_PROMPT` stay byte-identical (the sole frozen-prefix
change is the new tool schema — once at deploy, then stable); the do-not-substitute rule
travels *with the identity block* in the user turn — no system edit. `run_cycle(...)` and
`_cycle_prompt(...)` gain `instrument_identities: dict[str, str] | None = None`; when
non-empty, this line (omitted otherwise) renders immediately after the `Watchlist:` line,
`"; "`-joining sorted `f"{sym} = {desc}"` pairs:
```
Instrument identities (resolved from live company profiles — analyze ONLY these instruments; a symbol not listed is unresolved, ticker-only: never infer its company from memory or substitute a different company/ticker): AAPL = Apple Inc (NASDAQ NMS - GLOBAL MARKET, equity); MSFT = ...
```
`app.py run_review_cycle` passes `instrument_identities=await
self._instrument_identities(self.config.all_watchlist_symbols())` — a new helper returning
`{}` when `not self.config.ai.snapshot.identity`, else mapping each symbol via `await
self.router.profile(sym)` under a per-symbol `try/except Exception: continue` (fail open) to
`f"{prof.name} ({prof.exchange}, {prof.asset_type})"`. First cycle pays ≤1 HTTP call per
symbol; the weekly cache makes later cycles free. It can never block or fail a cycle.

### 3.6 Technical-analyst indicator guidance — `ai/analysis/analysts.py`
Analyst role prompts are static (per-symbol data rides the user turn), so guidance belongs
in `_ROLES["technical"]` — cache-safe. Replace it with (actual text):
```
You are the TECHNICAL analyst. Judge trend, momentum, and levels using ONLY the snapshot's
fixed indicators, with these usage rules:
- SMA50/SMA200: trend filters; price above both = uptrend context. The 50/200 cross flags a
  regime change but LAGS badly — never time an entry off the cross alone.
- EMA10: short-term momentum; whipsaws in ranges — read it only with the larger trend.
- MACD(12,26,9): momentum inflection; shrinking histogram = fading momentum. Unreliable in
  sideways chop — a bare crossover is not a signal without trend confirmation.
- RSI14: >70 stretched / <30 washed out, BUT it stays pinned for weeks in strong trends —
  treat extremes as risk context, not a standalone reversal call.
- Bollinger(20,2): %B near 1 = at upper band; trending price "walks the band" (not a sell),
  in ranges the bands are mean-reversion bounds. Band width is volatility, not direction.
- ATR14: a volatility unit for stop distance and sizing — it has NO directional content.
- N/A means insufficient data: record it in data_gaps; NEVER estimate a missing indicator or
  derive indicator values yourself from the closes.
```

## 4. Failure modes (all fail open or explicit — never a synthesized number)
- **Quote missing/stale:** analyst path → `None`, symbol skipped (unchanged contract); tool
  path → `DataError` envelope with the data_gaps instruction.
- **Bars missing/short:** snapshot survives; OHLCV `null`, per-indicator `"N/A ..."` — never estimated.
- **Profile missing (ETF/crypto/outage/no PROFILE provider):** ticker-only identity line
  (§3.3); `router.profile` → `None`, negative-cached 1 h; `_instrument_identities` omits the
  symbol (the §3.5 block labels unlisted symbols ticker-only).
- **Malformed provider data:** router already drops unsound bars (`_bar_is_sound`) and fails
  over `ValueError`/`InvalidOperation`; nothing new downstream.

## 5. Safety-invariant checklist for reviewers
1. **Advisory-only upstream:** `Snapshot`/`InstrumentProfile` flow only into analyst user
   content, `_cycle_prompt`'s user turn, and tool-result JSON — grep proves no import in
   `risk/`/`execution/`/`portfolio/`; `RiskEngine`/`OrderManager`/`submit_decision` untouched.
2. **Prompt cache:** `SYSTEM_PROMPT`/`CHAT_SYSTEM_PROMPT` byte-identical to v2.12.1; per-cycle
   identity/indicator text only in user content; frozen-prefix deltas are deploy-time
   constants only (tool schema, technical role string).
3. **Decimal exactness:** no format spec or `float()` on any Money value in render/payload;
   a test pins `"190.10"` verbatim. Derived indicators labeled and `f"{v:.4f}"`-formatted.
4. **No new AI cost:** zero added model calls; tool deterministic; ships ON per §3.2;
   `ai.snapshot.identity: false` removes identity lookups entirely.
5. **Never-guess:** every degrade path emits `N/A`/`unresolved`/`DataError` — no defaults.
6. **Anthropic-native, no new deps;** bounded tool payload (≪ 60 KB cap).

## 6. TDD task list (ordered; write the named tests first, watch them fail, then implement)
1. **Profile model + capability.** Tests `tests/unit/test_data_profile.py`:
   `test_profile_capability_exists`, `test_base_provider_profile_raises_not_implemented`,
   `test_instrument_profile_model_uppercases_and_stamps`. Impl: `core/models.py`,
   `data/base.py`. Done: tests green; `mypy --strict` clean.
2. **Finnhub profile2.** Tests (same file): `test_finnhub_profile_parses_profile2` (mock
   `_get_json`), `test_finnhub_profile_empty_raises_nonretryable`,
   `test_finnhub_advertises_profile_capability`. Impl: `data/providers/finnhub.py`.
3. **Router cache + fail-open.** Tests in `tests/unit/test_data_router.py`:
   `test_profile_cached_for_a_week` (provider called once across repeated hits),
   `test_profile_negative_cache_retries_hourly`,
   `test_profile_returns_none_when_all_providers_fail`. Impl: `data/router.py`.
4. **SnapshotConfig + enriched builder (incl. `q.price` bug fix).** Tests (rewrite fakes in
   `tests/unit/test_analysis_snapshot.py` to real `Quote`/`Bar` models):
   `test_snapshot_uses_quote_last_with_real_quote_model`, `test_renders_decimals_exactly`,
   `test_latest_ohlcv_row_and_closes_oldest_first`, `test_indicators_na_never_estimated`,
   `test_survives_bars_failure`, `test_identity_line_and_ticker_only_fail_open` (incl.
   router without `.profile`), `test_payload_structure_sources_and_note`. Impl:
   `core/config.py`, `ai/analysis/snapshot.py`. Done: old substring tests still pass.
5. **`get_market_snapshot` tool.** Tests `tests/unit/test_tool_market_snapshot.py`:
   `test_tool_returns_payload_with_exact_price_strings`, `test_tool_records_sources_used`,
   `test_tool_raises_data_error_without_quote`, `test_tool_respects_allow_delayed`,
   `test_schema_in_data_tools_and_all_tools_with_source_of_truth_footer`. Impl:
   `ai/schemas.py`, `ai/tools.py`. Done: chat tool set inherits it via `DATA_TOOLS`.
6. **Identity into `_cycle_prompt`/`run_cycle`/app.** Tests
   `tests/unit/test_identity_injection.py`: `test_identity_block_rendered_in_user_text`,
   `test_no_block_when_empty_or_none`, `test_system_prompt_byte_identical`,
   `test_app_helper_fails_open_per_symbol`, `test_helper_disabled_by_config_flag`.
   Impl: `ai/agent.py`, `app.py`. Done: wiring test green.
7. **Technical-analyst guidance.** Tests (extend `tests/unit/test_analysis_analysts.py`):
   `test_technical_role_carries_indicator_guidance`, `test_guidance_absent_from_other_roles`.
   Impl: `ai/analysis/analysts.py` (§3.6 text). Done: 4-role degradation test still green.
8. **Wiring + isolation sweep.** Tests `tests/unit/test_snapshot_wiring.py`:
   `test_dispatchers_and_analysis_service_get_snapshot_config` (both `ToolDispatcher(...)`
   sites + `AnalysisService`), `test_snapshot_and_profile_never_imported_by_risk_or_execution`
   (source scan mirroring `test_analysis_wiring.py`). Impl: `app.py`, `ai/analysis_service.py`
   (pass `snapshot_config` through). Done: full `ruff` + `mypy --strict` + `pytest` gate.

## 7. Existing tests that may break (and why that's expected)
- `tests/unit/test_analysis_snapshot.py` — fakes define `.price` (the bug's camouflage); rewritten in task 4 to real-`Quote`-shaped fakes. Substring assertions survive.
- `tests/unit/test_analysis_service.py` — fake quote is `price = 190.1` (a float!); update
  to `last=Decimal("190.10")`. Pipeline behavior assertions unaffected.
- `tests/unit/test_analysis_analysts.py` — positional `Snapshot(...)` ctor keeps working
  (new fields are trailing defaults); role-set assertions unaffected by prompt text.
- Safe by construction: `test_agent_parsing.py` (counts `submit_decision` once),
  `test_account_chat.py` (asserts absence, not a closed tool set), `test_analysis_wiring.py`
  (kwarg-based `_cycle_prompt` calls; AST whitelist keyed to `analysis_packets` only).
