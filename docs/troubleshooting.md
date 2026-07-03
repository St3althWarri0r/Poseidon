# Troubleshooting

First stop, always:

```bash
aegis doctor                       # config, vault, credentials, calendar, DB
journalctl --user -u aegis-trader -e --no-pager   # service logs
tail -f ~/.local/share/aegis-trader/logs/aegis.jsonl
```

## Startup

**`no vault at ...` / `vault is locked`** — run `aegis vault init`; for
the service, store the passphrase as a systemd credential
(docs/security.md). Interactive runs prompt automatically.

**`wrong passphrase or corrupt vault`** — the passphrase is wrong, or the
file was damaged. There is no recovery without the passphrase (by design);
restore from backup or `rm vault.bin && aegis vault init` and re-enter
keys.

**`invalid configuration`** — the error names the exact field; `aegis
config validate` reproduces it without starting anything.

**`audit chain verification FAILED at seq N`** — the audit table was
modified outside the app (or disk corruption). Aegis refuses to start.
Investigate first (`aegis audit tail`); if you accept losing history,
archive the DB file and start fresh.

**`unknown broker '...'` / `unknown strategy '...'`** — the error lists
valid names; check spelling in config.

## Data

**`all providers failed for 'quotes'`** — keys missing/expired
(`aegis vault list`), rate limits exhausted, or network down. The
dashboard's *Data providers* card shows per-provider penalty state and
latency. Trading pauses by design until data returns.

**`quote ... is stale — refusing to use it`** — the provider is serving
old data (common: Alpha Vantage EOD quotes, or a free-tier delayed feed).
Add a real-time source (Polygon/Alpaca/Tradier) for anything that must
trade. Delayed data can still inform research if
`data.allow_delayed_for_research: true`.

**AI decisions list `data_gaps`** — expected behavior when a capability
has no configured provider (e.g. no `finnhub` = no earnings calendar). Add
a provider covering the gap.

## Brokers

**`Broker disconnected` notifications** — the sync service retries with
backoff automatically and notifies again on reconnect. Check the broker's
own status page; check credentials.

**Schwab `token refresh failed`** — the 7-day refresh token expired.
Re-run the consent flow and update the vault (docs/broker-setup.md).

**IBKR `gateway is not authenticated`** — open the gateway URL in a
browser and log in again; gateway sessions expire periodically per IBKR
policy.

**`BrokerNotSupportedError`** — you configured a stub (Fidelity, M1,
Robinhood, …). See the API status matrix in docs/broker-setup.md.

## Trading behavior

**"It never trades!"** — check, in order: mode (research never trades),
market session, circuit breaker (dashboard header), risk meters at their
limits, the reasoning log (Claude often decides no action and says why),
and `data_gaps` in recent decisions.

**`risk rule '...' : ...` rejections** — working as intended; the message
carries the numbers. Adjust `risk:` limits deliberately if they're too
tight for your account size (e.g. `min_order_notional` vs. small
accounts).

**Circuit breaker open** — the reason is in the header tooltip and the
event feed. Error-rate trips clear after the cooldown; manual halts need
*Resume*; audit-integrity halts need investigation.

**Orders stuck `submitted`/`accepted`** — normal for non-marketable limit
orders; they're polled all session and expire per their time-in-force.
Cancel from the Orders table if unwanted.

## AI

**`Anthropic authentication failed`** — key missing/rotated:
`aegis vault set anthropic_api_key`.

**`rate limited after SDK retries`** — lower the review cadence
(`ai.review_interval_seconds`) or raise your Anthropic tier.

**Cycles hit the tool-iteration limit** — raise
`ai.max_tool_iterations`, shrink the watchlist, or lower `ai.effort`; the
cycle safely ends as no-action when the limit hits.

## Dashboard

**Nothing on :8321** — is the process running? A changed
`dashboard.port`? Another process on the port (`ss -tlnp | grep 8321`)?

**Remote access** — deliberately not supported directly; tunnel:
`ssh -L 8321:127.0.0.1:8321 yourhost`.

## Holiday calendar warning

`holiday calendar does not cover today` (health card red, market treated
as closed): you're running a build older than the shipped calendar years.
`aegis update apply` or pull the latest — the calendar ships in
`core/clock.py` and is maintained two years ahead.
