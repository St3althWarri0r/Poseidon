# Broker setup

## API status matrix

Poseidon integrates **only** through official APIs or officially supported
automation interfaces. Where none exists, the plugin is a documented stub
that refuses to operate — Poseidon never screen-scrapes or drives private
endpoints.

| Broker | Official retail trading API? | Poseidon status |
| --- | --- | --- |
| Poseidon Paper | built-in simulator | ✅ full plugin (`paper`) |
| Alpaca | yes (Trading API, paper env) | ✅ full plugin (`alpaca`) |
| Tradier | yes (Brokerage API, sandbox) | ✅ full plugin (`tradier`) |
| tastytrade | yes (Open API, cert env) | ✅ full plugin (`tastytrade`) |
| Charles Schwab | yes (Trader API — individuals) | ✅ full plugin (`schwab`) |
| Interactive Brokers | yes (Client Portal Gateway) | ✅ full plugin (`ibkr`) |
| Public.com | yes (Trading API, free access) | ✅ full plugin (`public`) |
| E*TRADE | yes, but OAuth 1.0a with daily re-auth | 🧩 documented scaffold (`etrade`) |
| Webull | OpenAPI exists, application-gated | 🧩 documented scaffold (`webull`) |
| Robinhood | crypto only; no equities API | ⛔ stub (`robinhood`) |
| Fidelity | no self-service API | ⛔ stub (`fidelity`) |
| M1 Finance | no API of any kind | ⛔ stub (`m1finance`) |
| Vanguard | no API of any kind | ⛔ stub (`vanguard`) |

Scaffold/stub plugins raise a clear error explaining why, with the
integration checklist in the module docstring
(`src/poseidon/brokers/plugins/<name>.py`). Implementing one is a
single class — see docs/plugin-development.md.

Exactly one enabled broker is marked `primary: true`; all orders route to
it. Credentials are JSON objects stored in the vault under the name given
in `brokers[].credential`.

**The easy path: the dashboard's Account view.** Every full plugin below
can be connected from the UI — pick the broker, enter the fields, *Test
connection*, then *Connect & switch*. Credentials are written to the
encrypted vault, the choice is persisted to `poseidon.local.yaml` (merged
over `poseidon.yaml` at startup; delete it to revert), and the active
broker is swapped live. The YAML instructions in each section below remain
the equivalent manual path — Schwab's one-time OAuth browser consent (see
its section below) still happens outside Poseidon before its refresh token
can be pasted into either path.

## Paper (built-in)

```yaml
brokers:
  - name: paper
    primary: true
    options: { starting_cash: 100000 }
```

Fills are priced from **live quotes** via the data router (ask for buys,
bid for sells; marketable-limit logic for limit orders). State persists to
`paper_state.json` in the data dir. No credentials needed.

## Alpaca

1. Create an account at alpaca.markets; generate API keys (paper keys from
   the paper dashboard, live keys from the live dashboard).
2. `poseidon vault set alpaca_keys` with value:
   `{"key_id": "AK...", "secret_key": "..."}`
3. Config:

```yaml
brokers:
  - name: alpaca
    primary: true
    paper: true          # paper-api.alpaca.markets
    credential: alpaca_keys
```

Capabilities: equities (fractional), options, crypto, extended hours,
margin, client order IDs.

## Tradier

1. developer.tradier.com → create an access token (sandbox tokens from the
   sandbox dashboard).
2. `poseidon vault set tradier_creds` with
   `{"access_token": "...", "account_id": "VA000000"}`
3. Config: `name: tradier`, `paper: true` targets the sandbox host.

## tastytrade

1. Open API access is enabled per account at developer.tastytrade.com.
2. `poseidon vault set tasty_creds` with
   `{"username": "...", "password": "...", "account_number": "5WX00000"}`
   (a `remember_token` can replace the password after first login).
3. `paper: true` targets the certification environment.

## Charles Schwab

Schwab's Trader API for individuals uses OAuth2 with a 7-day refresh token
and a one-time interactive browser consent:

1. developer.schwab.com → create an "individual developer" app with the
   Trader API product; note the app key + secret and set the callback URL
   to `https://127.0.0.1:8182`.
2. Run the authorization flow once (any OAuth helper works; the flow is:
   authorize URL → login+consent → code → token exchange). Retrieve your
   `refresh_token`, then call `GET /trader/v1/accounts/accountNumbers` for
   the `hashValue` of your account.
3. `poseidon vault set schwab_creds` with
   `{"app_key": "...", "app_secret": "...", "refresh_token": "...",
     "account_hash": "..."}`

Poseidon refreshes the 30-minute access token automatically. When the 7-day
refresh token expires, you must repeat step 2 (Schwab requires the human
consent; Poseidon will alert you rather than fake it). Schwab has no paper
environment — the `paper` flag is ignored.

## Interactive Brokers

IBKR's supported self-hosted path is the **Client Portal Gateway** — a
small local process you log into via a browser:

1. Download the gateway from IBKR ("Client Portal API Gateway"), run it
   (`bin/run.sh root/conf.yaml`), and log in at `https://localhost:5000`.
2. `poseidon vault set ibkr_creds` with `{"account_id": "U1234567"}` (or `{}`
   to auto-select the session's first account).
3. Config:

```yaml
brokers:
  - name: ibkr
    primary: true
    credential: ibkr_creds
    options: { gateway_url: "https://localhost:5000", verify_ssl: false }
```

Poseidon tickles the gateway session from its health loop to keep it alive;
gateway logins still expire periodically per IBKR policy (re-login in the
browser when the broker probe goes red). Paper vs live is a property of
which account you log the gateway into.

## Public.com

Public's Trading API is free to enable and covers stocks/ETFs
(fractional), single- and multi-leg options, and crypto — the same
official API behind Public's own MCP integration, driven here natively so
trading stays fully autonomous (no chat client in the loop).

1. In the Public app/site: Settings → Security & privacy → API access →
   generate an API **secret key**.
2. `poseidon vault set public_api_secret` with
   `{"secret": "...", "account_id": "..."}` (`account_id` optional — the
   first account on the key is used).
3. Config:

```yaml
brokers:
  - name: public
    primary: true
    paper: false           # required — see below
    credential: public_api_secret
```

Behavior notes:

- **No paper environment.** Public offers none, so the plugin refuses
  `paper: true` instead of silently trading live — you must write
  `paper: false` deliberately. Use the built-in `paper` broker for
  simulation.
- GTC orders are mapped to Public's GTD with a 30-day window (Public
  supports DAY and GTD).
- Multi-leg option orders must be LIMIT with a net price (Public rule).
- The same secret powers the free `public_data` market data provider
  (docs/api-configuration.md) — one credential, trading + data.
