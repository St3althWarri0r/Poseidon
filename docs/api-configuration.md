# API configuration

## Anthropic (the decision engine)

1. Create an API key at <https://console.anthropic.com>.
2. `aegis vault set anthropic_api_key`
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

## Market data providers

Configure several — failover is automatic and free. Priorities decide the
order; a failing provider is penalized (exponential backoff) and traffic
shifts to the next one.

| Provider | Capabilities in Aegis | Key from | Vault value |
| --- | --- | --- | --- |
| `public_data` | real-time quotes, bars, option chains + greeks, crypto | public.com (API secret) | secret, or `{"secret": "...", "account_id": "..."}` |
| `polygon` | quotes (NBBO), bars, option chains + greeks, news | polygon.io | plain API key |
| `finnhub` | quotes, news, earnings calendar, economic calendar, sector taxonomy | finnhub.io | plain API key |
| `twelvedata` | quotes, bars | twelvedata.com | plain API key |
| `alphavantage` | EOD quotes, daily bars, news+sentiment | alphavantage.co | plain API key |
| `alpaca` | quotes, bars, option chains, news | alpaca.markets | `{"key_id": "...", "secret_key": "..."}` |
| `tradier_data` | quotes, daily bars, option chains + greeks | tradier.com | access token (options: `{sandbox: true}`) |

### Running on $0 of API subscriptions

The platform is designed so the only thing you pay for is Claude:

- **`public_data` is free** with a Public brokerage account and serves
  real-time quotes, bars, and full option chains with greeks — the same
  API secret as the `public` broker. If you trade through Public, this is
  your primary source and costs nothing.
- **`finnhub`**, **`twelvedata`**, and **`alphavantage`** free tiers cover
  news, both calendars, and backup quotes/bars.
- **`alpaca`** (IEX feed) and **`tradier_data`** (sandbox) are also free
  with their respective accounts.

Recommended free stack: `public_data` (priority 10) for quotes/options/
bars + `finnhub` (priority 20) for news and both calendars, with
`twelvedata`/`alphavantage` as failover. Paid sources (e.g. `polygon`)
are optional upgrades, never requirements.

Notes:

- Free tiers are rate-limited; Aegis honors `Retry-After` and backs off,
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

All keys go in the encrypted vault (`aegis vault set NAME`), referenced
from config by name. Keys are never written to config, logs (a redaction
processor scrubs anything key/token/secret-shaped), or the database.
