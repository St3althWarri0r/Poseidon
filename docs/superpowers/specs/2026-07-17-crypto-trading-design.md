# Crypto Trading Support (Paper) — Design Spec

Date: 2026-07-17
Status: Design (paper-first; live crypto is a documented follow-on)
Scope: Let the operator place and simulate crypto orders (e.g. `BTC/USD`) 24/7 on the
paper broker, so the order path can be exercised outside equity market hours.

## 1. Goal & context

A `BTC/USD` paper buy failed with "no quote available — Not Found". That message is
`app.js:785` wrapping a 503 from `/api/quote/{symbol}`, whose provider raised
`ProviderError("HTTP 404 …")` because the crypto symbol was routed to Alpaca's
**stocks** endpoint. This spec makes the full path — quote → risk → paper fill — work
for spot crypto pairs, changing nothing on the equity path.

Non-goals: new crypto exchange/broker; changing the AI watchlist (equities today);
USDT/stablecoin-quoted pairs. Supported form is `BASE/USD` (BTC/USD, ETH/USD, …).

## 2. Diagnosis verified against the code (2 of 3 blockers are stale)

| Diagnosis claim | Reality in code |
|---|---|
| No provider can quote crypto | **TRUE.** `alpaca_data.py` `quote()`:63 hardcodes `/v2/stocks/{symbol}/quotes/latest`, `bars()`:87 `/v2/stocks/{symbol}/bars`. `public_data.py` can (via `options.crypto_symbols`) but is disabled in the user's config. |
| Paper broker lacks CRYPTO capability | **TRUE.** `paper.py` `capabilities()`:90-99 = {EQUITIES, FRACTIONAL_SHARES, EXTENDED_HOURS, PAPER_TRADING, TAX_LOTS}. `manager.py:529-530` already rejects a CRYPTO order when the broker lacks `BrokerCapability.CRYPTO` — that check **already exists**. |
| No crypto symbol/asset-class handling; MarketOpenRule must be exempted | **PARTLY STALE.** `AssetClass.CRYPTO`/`BrokerCapability.CRYPTO`/`DecisionAction` all exist. `/api/trade`:395 defaults `asset_class="equity"` and nothing derives it from a symbol — TRUE. **But `MarketOpenRule` (rules.py:148-150) ALREADY exempts crypto** (`if ctx.order.asset_class is AssetClass.CRYPTO: return`). The 24/7 gate is done in code; it just lacks a test and never fires because no order is ever tagged CRYPTO. |

Additional blocker not in the diagnosis: **`/api/quote/{symbol}` (server.py:351) is a
path parameter** — `BTC/USD` contains a slash the `{symbol}` converter will not match,
so the ticket "Quote" button cannot fetch a crypto quote even after routing is fixed.

Also already present: the **live `alpaca.py` broker plugin advertises
`BrokerCapability.CRYPTO`** (alpaca.py:85) and maps `crypto` positions (l.127), so the
live follow-on is mostly wiring + tests, not new order code.

## 3. Design

### A. Canonical symbol format + detection helper — `core/symbols.py` (new)

Internal canonical form: **`BASE/QUOTE`, uppercase, exactly one `/`** (e.g. `BTC/USD`).
This matches BOTH Alpaca's crypto data API (`v1beta3`, `symbols=BTC/USD`) and the Alpaca
trading API, so no per-layer remapping. `Quote.symbol`'s validator already `.strip().upper()`s,
and `BTC/USD` is slash-safe under `.upper()`.

Lives in `core/` (not `data/`) because it is a pure domain classification used by three
layers (data routing, execution/api order tagging, provider parsing) and `core` may not
import them. Signatures:

```python
# src/poseidon/core/symbols.py
_CRYPTO_RE = re.compile(r"^[A-Z0-9]{1,15}/[A-Z]{3,5}$")
SUPPORTED_CRYPTO_QUOTES: frozenset[str] = frozenset({"USD"})  # stablecoins excluded

def is_crypto_symbol(symbol: str) -> bool:
    """True iff `symbol` is a crypto PAIR (contains one '/'). No equity ticker
    contains '/', so this is a conservative, maintenance-free routing signal."""
    return bool(_CRYPTO_RE.match(symbol.strip().upper()))

def asset_class_for_symbol(symbol: str) -> AssetClass:
    return AssetClass.CRYPTO if is_crypto_symbol(symbol) else AssetClass.EQUITY

def normalize_crypto_symbol(symbol: str) -> str:
    """Canonicalize + reject unsupported pairs with a clean error. USDT/USDC and
    any non-USD quote raise UnsupportedSymbolError (a PoseidonError subclass)."""
    s = symbol.strip().upper()
    base, _, quote = s.partition("/")
    if not base or quote not in SUPPORTED_CRYPTO_QUOTES:
        raise UnsupportedSymbolError(
            f"{symbol!r}: only BASE/USD crypto pairs are supported "
            f"(stablecoin/{quote or '?'}-quoted pairs are not)")
    return s
```

Detection = "has a slash" (routing + asset-class). Supported-pair validation (USD quote)
is a separate guard so a fat-fingered `BTC/USDT` gives a clear rejection, not a 404. A new
`UnsupportedSymbolError(PoseidonError, retryable=False)` goes in `core/errors.py`.

### B. `DataCapability.CRYPTO` + router routing — `data/base.py`, `data/router.py`

Add `CRYPTO = "crypto"` to `DataCapability`. Teach the router to require it for crypto
symbols so a crypto quote can NEVER reach an equity-only provider (the current bug):

```python
# data/router.py — add an optional extra requirement to the capable filter
async def _route(self, capability, call, *, require: DataCapability | None = None):
    capable = [s for s in self._slots
               if capability in s.provider.capabilities()
               and (require is None or require in s.provider.capabilities())]
    ...  # unchanged: penalty box, two-pass failover, error handling
```

`quote()`/`bars()` compute the requirement from the symbol:

```python
req = DataCapability.CRYPTO if is_crypto_symbol(symbol) else None
quote = await self._route(DataCapability.QUOTES, lambda p: p.quote(symbol), require=req)
```

Equity path is byte-for-byte unchanged (`require=None`). If no CRYPTO-capable provider is
configured, `_route`'s existing empty-`capable` branch raises `DataUnavailableError`
("no configured provider supports …") — no-data-no-trade, honestly.

### C. Alpaca crypto data — `data/providers/alpaca_data.py`

`capabilities()` gains `DataCapability.CRYPTO` (Alpaca serves crypto free with the same
`alpaca_keys`; keep QUOTES/BARS/OPTIONS/NEWS). Branch `quote()`/`bars()` on
`is_crypto_symbol(symbol)` to the `v1beta3` crypto routes. The crypto API is **multi-symbol
and keyed by symbol** — different shape from stocks:

```python
# quote (crypto): GET /v1beta3/crypto/us/latest/quotes?symbols=BTC/USD
#   -> {"quotes": {"BTC/USD": {"bp":.., "ap":.., "bs":.., "as":.., "t":".."}}}
async def quote(self, symbol):
    if not is_crypto_symbol(symbol):
        ...  # existing /v2/stocks/... path, unchanged
    sym = normalize_crypto_symbol(symbol)
    payload = await self._get("/v1beta3/crypto/us/latest/quotes", symbols=sym)
    q = (payload.get("quotes") or {}).get(sym)
    if not q or not q.get("t"):
        raise ProviderError(self.name, f"no quote for {sym}")
    as_of = datetime.fromisoformat(q["t"].replace("Z", "+00:00"))
    return Quote(symbol=sym,
                 bid=Decimal(str(q["bp"])) if q.get("bp") else None,
                 ask=Decimal(str(q["ap"])) if q.get("ap") else None,
                 bid_size=q.get("bs"), ask_size=q.get("as"),
                 as_of=as_of, source=self.name)

# bars (crypto): GET /v1beta3/crypto/us/bars?symbols=BTC/USD&timeframe=1Day&limit=..&start=..
#   -> {"bars": {"BTC/USD": [{"o","h","l","c","v","t"}, ...]}}  (reuse _TIMEFRAMES)
```

Prices parsed as `Decimal(str(...))` — crypto's many decimals never touch float. Crypto
`t`/`o`/`h`/`l`/`c`/`v` field names match stocks; confirm against the live API during impl.
No auth change (same `_headers`). Crypto has no OCC/options — leave `option_chain` as-is.

### D. Paper broker crypto — `brokers/plugins/paper.py`

Add `BrokerCapability.CRYPTO` to `capabilities()`. **No fill-logic change needed:** fills
already price through `set_quote_fn` → `router.quote(symbol, allow_delayed=True)` (app.py:336),
which is crypto-aware after (B); `_try_fill`/`_mark_price` use `ask/bid/last/mid`, all present
on the crypto quote; positions/lots key on `.upper()` symbol; `FRACTIONAL_SHARES` already
allows `0.05 BTC`; money is `Decimal` throughout. (Cosmetic: `_realized_pnl_today` rolls on
the ET trading day even for 24/7 crypto — acceptable, note only.)

### E. Risk engine — the 24/7 crux — `risk/rules.py`

- **`MarketOpenRule`: already exempts crypto** (l.149-150). Verify + add the missing test.
- **`VolumeRule`: exempt crypto.** `min_avg_volume` defaults to 100,000 (equity *share*
  count). Crypto `Bar.volume` is in *coins* — BTC trades tens of thousands of coins/day
  (≈$30B notional) yet would fail a 100k-share floor. Applying a share-count floor to a
  coin count is a category error. Add `if ctx.order.asset_class is AssetClass.CRYPTO: return`
  (mirrors the existing OPTION exemption). Crypto liquidity stays gated by **`SpreadRule`**
  and **`SlippageProtectionRule`**, which use `spread_pct` (asset-class-neutral).
- **Everything else applies to crypto, unchanged:** `OrderNotionalRule` (a whole BTC > the
  $25k `max_order_notional` cap is *correctly* rejected → size fractionally), position/
  exposure/leverage caps, buying power, `VolatilityHaltRule` (crypto is volatile — keep it),
  cooldown, orders-per-day, daily/weekly loss, drawdown, VaR, econ blackout, `ReduceOnlyRule`,
  circuit breaker, `FreshPortfolioRule`. `SectorConcentrationRule`/`OptionsExposureRule`
  already self-skip non-EQUITY/non-OPTION. `RiskContext.notional` uses multiplier 1 for
  crypto (spot notional = qty×price) — correct.

Only two exemptions total: genuine market-hours and the share-count volume floor. Nothing else.

### F. Order path + dashboard ticket — `api/server.py`, `api/static/*`

1. **`/api/trade` (server.py:393-404):** when `asset_class` is absent, derive it —
   `asset_class = AssetClass(body["asset_class"]) if "asset_class" in body else
   asset_class_for_symbol(symbol)`; for a crypto symbol call `normalize_crypto_symbol` and
   422 on `UnsupportedSymbolError`. Explicit `asset_class` still honored.
2. **`/api/quote/{symbol}` → `/api/quote/{symbol:path}`** (server.py:351) so `BTC/USD`
   (the JS already `encodeURIComponent`s it) matches. This is the fix for the ticket Quote
   button on crypto.
3. **Ticket UI (index.html:126 / app.js:806):** add a format hint under Symbol
   ("Crypto: BASE/USD, e.g. BTC/USD"). The submit body omits `asset_class` today, so the
   server auto-detect (1) tags it — minimal JS change. Optional: an "Asset class:
   Auto/Equity/Crypto" select for explicitness.
4. **AI-proposed trades (secondary):** `_trade_to_order` (manager.py:170) copies
   `trade.asset_class` (defaults EQUITY). Add a `model_validator` on `ProposedTrade` (or a
   one-line upgrade in `_trade_to_order`) reusing `asset_class_for_symbol` so an AI-named
   crypto symbol is tagged CRYPTO. Secondary because the watchlist is equities; single
   shared helper keeps manual/API/AI consistent.

## 4. Config

No schema change — `ProviderConfig.options` is `dict[str, Any]`. Enable Alpaca data (already
the user's provider) and it advertises CRYPTO unconditionally. Optional `crypto_bases` allow-
list can live in provider `options` or a module default for nicer "unknown pair" errors; the
hard gate is the USD-quote check in `normalize_crypto_symbol`. Secrets: reuse the existing
`alpaca_keys` vault entry — no new credential.

## 5. Failure modes

- **Unknown pair (`FOO/USD`):** passes detection; Alpaca returns no quote → `ProviderError`
  → router failover → `AllProvidersFailedError`/`DataError` → order `REJECTED_RISK`
  ("required live data unavailable"). Clean, no trade.
- **Stablecoin/unsupported (`BTC/USDT`):** `normalize_crypto_symbol` raises
  `UnsupportedSymbolError` → 422 at `/api/trade` with a clear message; never reaches a broker.
- **Provider down:** normal penalty-box failover; if every CRYPTO-capable provider is down →
  `AllProvidersFailedError` → no trade (never a guessed price).
- **Broker lacks CRYPTO** (e.g. a future equity-only live broker): existing
  `_missing_capability` (manager.py:529) → `REJECTED_BROKER` "broker does not support crypto".
- **Crypto symbol on a stale/one-sided book:** `SpreadRule`/`SlippageProtectionRule` reject,
  same as equities.

## 6. Safety checklist

- [ ] Crypto exempt from **market-hours only** (`MarketOpenRule`) and the equity **share-
      count `VolumeRule`** (justified §E) — every other risk rule still runs for crypto.
- [ ] `ReduceOnlyRule` NOT exempted (the platform still never opens short crypto).
- [ ] Money is `Decimal` end-to-end; crypto prices parsed `Decimal(str(...))`, never float.
- [ ] Paper-only for now; live crypto needs a broker that advertises CRYPTO (Alpaca live
      already does — see §8).
- [ ] No equity regression: `require=None` for equities; alpaca_data equity branch untouched;
      router two-pass failover unchanged.
- [ ] Consequential actions audited — orders already audit via the existing manager path; no
      new consequential action introduced.
- [ ] Secrets stay in the vault (reuse `alpaca_keys`).

## 7. Ordered TDD task list (tests first, then impl)

1. **Symbol helper.** Tests (`tests/unit/test_symbols.py`): `is_crypto_symbol` True for
   BTC/USD, ETH/USD; False for AAPL, BRK.B, SPY; `asset_class_for_symbol` mapping;
   `normalize_crypto_symbol` upper/strip, rejects BTC/USDT & bare base. Impl:
   `core/symbols.py`, `UnsupportedSymbolError` in `core/errors.py`.
2. **Router crypto routing.** Tests (`test_data_router.py`): crypto symbol only served by a
   CRYPTO-capable fake; equity-only fake skipped for crypto; equity routing unchanged; all
   crypto providers fail → `AllProvidersFailedError`. Impl: `DataCapability.CRYPTO`
   (`data/base.py`); `require` param + `quote()`/`bars()` requirement (`data/router.py`).
3. **Alpaca crypto data.** Tests (`test_alpaca_data_crypto.py`, provider-fake/httpx pattern):
   parse `{"quotes":{"BTC/USD":{…}}}` and `{"bars":{"BTC/USD":[…]}}`; `capabilities()` ⊇
   CRYPTO; equity path unchanged. Impl: crypto branch in `alpaca_data.py`.
4. **Paper broker crypto.** Tests (`test_paper_broker.py`): `capabilities()` ⊇ CRYPTO; a
   `BTC/USD` fractional buy fills from a crypto quote; position/fill are `Decimal`. Impl:
   add `BrokerCapability.CRYPTO` in `paper.py`.
5. **Risk rules.** Tests (`test_risk.py`): `MarketOpenRule` passes a CRYPTO order when the
   session is CLOSED (weekend); `VolumeRule` passes low-coin-volume crypto; other rules STILL
   fire for crypto (notional cap rejects an oversized BTC buy; `SpreadRule` rejects a wide
   book). Impl: crypto exemption in `VolumeRule` (`rules.py`).
6. **Order-path detection.** Tests (`test_p1_manager.py` + a server test): `/api/trade`
   auto-tags BTC/USD CRYPTO; explicit `asset_class` honored; BTC/USDT → 422;
   `ProposedTrade`/`_trade_to_order` upgrades a crypto symbol. Impl: `server.py` `trade()`,
   optional `ProposedTrade` validator / `manager.py`.
7. **Quote route + ticket.** Tests: `GET /api/quote/BTC/USD` returns a crypto quote. Impl:
   `{symbol:path}` in `server.py`; symbol hint in `index.html`; verify `app.js` submit still
   works (server auto-detects).
8. **Config/docs + enablement.** Confirm Alpaca data advertises CRYPTO; document
   `BASE/USD`-only; note the live follow-on. Files: `docs/`, sample `poseidon.yaml`.

## 8. Existing tests that may change

- Provider capability assertions (`test_config_alpaca_provider.py` and any exact-set check on
  `AlpacaDataProvider.capabilities()`) — now includes CRYPTO.
- Paper broker capability set assertions (`test_paper_broker.py` / `test_p1_paper.py`) — add CRYPTO.
- Router tests that build fakes/assert routing — a CRYPTO-capable fake may be needed; `_route`
  gains a keyword-only `require` (default None, so existing callers/tests are unaffected).
- `test_risk.py` — new crypto cases for `MarketOpenRule`/`VolumeRule`; existing equity cases pass.

## 9. Open questions / risks / live follow-on

- **`{symbol:path}` + `%2F`:** confirm Starlette decodes `encodeURIComponent("BTC/USD")`
  ("BTC%2FUSD") so `{symbol:path}` matches; if not, switch `/api/quote` to a `?symbol=` query
  param. (Primary risk — verify empirically.)
- **Detection breadth:** slash-form for routing + USD-quote guard is deliberately conservative;
  a base allow-list is optional polish, not required.
- **Alpaca crypto free feed / field names:** verify `us` feed needs no extra entitlement and the
  crypto JSON field keys match stocks during impl (low risk).
- **`public_data` as a 2nd crypto provider:** could advertise CRYPTO, but its `crypto_symbols`
  option is keyed by bare base, not `BASE/USD` — leave primary crypto to `alpaca_data`.
- **Live crypto follow-on:** the live `alpaca.py` broker already advertises CRYPTO and maps
  crypto positions; remaining work is verifying `submit_order` passes `BTC/USD` verbatim and
  that Alpaca crypto TIF is `gtc`/`ioc` (not `day`/`opg`/`cls`). Paper ignores TIF nuance, so
  paper is unaffected. No new order code — mostly wiring + tests.
