# API configuration

## Anthropic (the decision engine)

1. Create an API key at <https://console.anthropic.com>.
2. `poseidon vault set anthropic_api_key`
3. `ai.model` defaults to `claude-opus-4-8`. `ai.effort` trades depth for
   cost/latency per review cycle (`high` recommended; `xhigh`/`max` for
   the most careful reasoning).

Cost control: the review cadence (`ai.review_interval_seconds`, default 5
minutes during market hours) is the dominant cost lever, followed by
`effort` and the watchlist size. The system prompt is cache-controlled, so
frequent cycles benefit from prompt caching automatically.

The agent's system prompt bans acting on remembered or estimated market
data; every tool it can call is wired to the live data router, and its
final decision must arrive through a strict-schema tool call.

## Local model backend (no API credit)

To run the portfolio manager for free on a local, OpenAI-compatible endpoint
(e.g. [LM Studio](https://lmstudio.ai)) instead of the Anthropic API, set the
`ai` backend:

```yaml
ai:
  backend: openai_compatible
  base_url: http://localhost:1234/v1
  model: devstral-small-2-24b-instruct-2512   # any tool-use-capable local model
  temperature: 0.2
  input_price_per_mtok: 0     # local = free
  output_price_per_mtok: 0
```

The default (`backend: anthropic`) is unchanged, and it stays a one-line switch
back. Everything below the model — the audited tool loop, the strict
`submit_decision` schema, the risk engine — is identical; only what generates
the trade ideas changes. Local models make **weaker** decisions than Opus:
fine for paper experimentation, not for real money.

A local brain still needs **real-time quotes** to trade (orders require fresh
data no matter which model decides them), so enable the Alpaca IEX feed below —
the free finnhub/twelvedata/alphavantage tiers are too delayed to clear
`data.real_time_max_age_seconds`.

## Reflection → lesson-memory loop

When a position closes, Poseidon can distill a short **advisory lesson** (was the
call right, the realized return and alpha vs SPY, one actionable takeaway) and
re-inject the relevant recent lessons into future review cycles — a learning
loop on top of the audited decision record.

```yaml
ai:
  reflection:
    enabled: true        # write a lesson when a position closes
    inject: true         # feed relevant lessons into future cycle prompts
    max_injected: 8      # hard cap on lessons per cycle prompt
    per_symbol: 2        # newest lessons per relevant ticker
    global_n: 3          # newest lessons overall (cross-ticker)
    lookback_days: 120   # ignore lessons older than this
```

Lessons are **advisory context only**: they never gate or bypass the risk
engine, never enter the order path, and are kept out of the tamper-evident audit
chain (their own `trade_lessons` table). `inject: false` keeps writing lessons
but stops feeding them to the model (a reviewable ledger); `enabled: false`
turns the loop off. Use `inject: false` if you want to eyeball a weaker local
model's lesson quality before it influences decisions.

## Model tiering (a utility model for the auxiliary roles)

The **trading decision** always runs on the primary `ai.model`. The tolerant
auxiliary roles — operator chat and the reflection lessons — can optionally run
on a cheaper/faster **utility** model:

```yaml
ai:
  model: claude-opus-4-8                     # the money decision + reviewer
  utility_model: claude-haiku-4-5-20251001   # operator chat + reflection lessons
```

The utility model uses the **same backend and endpoint** as the primary with only
the model swapped (Anthropic Opus→Haiku on one account, or a smaller local model
served at the same LM Studio endpoint). Leave `utility_model` unset — the default
— and every role shares the primary backend exactly as before.

The trading agent and the algorithm reviewer are **never** handed the utility
backend; the money decision runs on the strong model, always. Tiering is a
cost/latency optimization for the advisory roles (and the seam a future
multi-analyst step can build on). On a local-only setup it is near-zero immediate
value — a smaller local model is still $0 — so its real payoff is the Anthropic
path.

## Market data providers

Configure several — failover is automatic and free. Priorities decide the
order; a failing provider is penalized (exponential backoff) and traffic
shifts to the next one.

| Provider | Capabilities in Poseidon | Key from | Vault value |
| --- | --- | --- | --- |
| `public_data` | real-time quotes, bars, option chains + greeks, crypto | public.com (API secret) | secret, or `{"secret": "...", "account_id": "..."}` |
| `polygon` | quotes (NBBO), bars, option chains + greeks, news | polygon.io | plain API key |
| `finnhub` | quotes, news, earnings calendar, economic calendar, sector taxonomy | finnhub.io | plain API key |
| `twelvedata` | quotes, bars | twelvedata.com | plain API key |
| `alphavantage` | EOD quotes, news+sentiment (bars not offered: free series is split-unadjusted) | alphavantage.co | plain API key |
| `alpaca` | quotes, bars, option chains, news | alpaca.markets | `{"key_id": "...", "secret_key": "..."}` |
| `tradier_data` | quotes, daily bars, option chains + greeks | tradier.com | access token (options: `{sandbox: true}`) |

### Running on $0 of API subscriptions

The platform is designed so the only thing you pay for is Claude:

- **`public_data` is free** with a Public brokerage account and serves
  real-time quotes, bars, and full option chains with greeks — the same
  API secret as the `public` broker. If you trade through Public, this is
  your primary source and costs nothing.
- **`finnhub`**, **`twelvedata`**, and **`alphavantage`** free tiers cover
  news, both calendars, and backup quotes (plus bars from `twelvedata`;
  Alpha Vantage bars are not offered — its free daily series is
  split-unadjusted and would change the price basis on failover).
- **`alpaca`** (IEX feed) and **`tradier_data`** (sandbox) are also free
  with their respective accounts.

Recommended free stack: `public_data` (priority 10) for quotes/options/
bars + `finnhub` (priority 20) for news and both calendars, with
`twelvedata`/`alphavantage` as failover. Paid sources (e.g. `polygon`)
are optional upgrades, never requirements.

Notes:

- Free tiers are rate-limited; Poseidon honors `Retry-After` and backs off,
  and the failover router shifts traffic automatically when a provider
  is penalized.
- `public_data` options: `{crypto_symbols: [BTC, ETH]}` marks watchlist
  symbols that should be quoted as crypto instruments.
- Alpha Vantage quotes are end-of-day: the freshness policy grades them
  DELAYED/STALE, so they can inform research but never orders — that is by
  design, not a bug.
- Capability gaps are fine: the router only asks a provider for what it
  advertises. If *no* configured provider covers a capability the AI asks
  for, the tool returns an explicit "unavailable" error and the AI must
  record it in `data_gaps` instead of guessing.

## Where keys live

All keys go in the encrypted vault (`poseidon vault set NAME`), referenced
from config by name. Keys are never written to config, logs (a redaction
processor scrubs anything key/token/secret-shaped), or the database.
