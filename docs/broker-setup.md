# Broker setup

## API status matrix

Aegis integrates **only** through official APIs or officially supported
automation interfaces. Where none exists, the plugin is a documented stub
that refuses to operate — Aegis never screen-scrapes or drives private
endpoints.

| Broker | Official retail trading API? | Aegis status |
| --- | --- | --- |
| Aegis Paper | built-in simulator | ✅ full plugin (`paper`) |
| Alpaca | yes (Trading API, paper env) | ✅ full plugin (`alpaca`) |
| Tradier | yes (Brokerage API, sandbox) | ✅ full plugin (`tradier`) |
| tastytrade | yes (Open API, cert env) | ✅ full plugin (`tastytrade`) |
| Charles Schwab | yes (Trader API — individuals) | ✅ full plugin (`schwab`) |
| Interactive Brokers | yes (Client Portal Gateway) | ✅ full plugin (`ibkr`) |
| Public.com | yes (new trading API) | 🧩 documented scaffold (`public`) |
| E*TRADE | yes, but OAuth 1.0a with daily re-auth | 🧩 documented scaffold (`etrade`) |
| Webull | OpenAPI exists, application-gated | 🧩 documented scaffold (`webull`) |
| Robinhood | crypto only; no equities API | ⛔ stub (`robinhood`) |
| Fidelity | no self-service API | ⛔ stub (`fidelity`) |
| M1 Finance | no API of any kind | ⛔ stub (`m1finance`) |

Scaffold/stub plugins raise a clear error explaining why, with the
integration checklist in the module docstring
(`src/aegis_trader/brokers/plugins/<name>.py`). Implementing one is a
single class — see docs/plugin-development.md.

Exactly one enabled broker is marked `primary: true`; all orders route to
it. Credentials are JSON objects stored in the vault under the name given
in `brokers[].credential`.

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
2. `aegis vault set alpaca_keys` with value:
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
2. `aegis vault set tradier_creds` with
   `{"access_token": "...", "account_id": "VA000000"}`
3. Config: `name: tradier`, `paper: true` targets the sandbox host.

## tastytrade

1. Open API access is enabled per account at developer.tastytrade.com.
2. `aegis vault set tasty_creds` with
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
3. `aegis vault set schwab_creds` with
   `{"app_key": "...", "app_secret": "...", "refresh_token": "...",
     "account_hash": "..."}`

Aegis refreshes the 30-minute access token automatically. When the 7-day
refresh token expires, you must repeat step 2 (Schwab requires the human
consent; Aegis will alert you rather than fake it). Schwab has no paper
environment — the `paper` flag is ignored.

## Interactive Brokers

IBKR's supported self-hosted path is the **Client Portal Gateway** — a
small local process you log into via a browser:

1. Download the gateway from IBKR ("Client Portal API Gateway"), run it
   (`bin/run.sh root/conf.yaml`), and log in at `https://localhost:5000`.
2. `aegis vault set ibkr_creds` with `{"account_id": "U1234567"}` (or `{}`
   to auto-select the session's first account).
3. Config:

```yaml
brokers:
  - name: ibkr
    primary: true
    credential: ibkr_creds
    options: { gateway_url: "https://localhost:5000", verify_ssl: false }
```

Aegis tickles the gateway session from its health loop to keep it alive;
gateway logins still expire periodically per IBKR policy (re-login in the
browser when the broker probe goes red). Paper vs live is a property of
which account you log the gateway into.
