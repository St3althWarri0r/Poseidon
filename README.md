# Poseidon

Autonomous AI trading platform for private, single-user operation on Linux.
Claude is the portfolio manager; every market fact it reasons about comes
from live, authoritative data providers — never from memory, never estimated.

```
┌────────────┐   signals   ┌──────────────┐  decisions  ┌─────────────┐  orders  ┌─────────┐
│ Strategy   │────────────▶│ Claude agent │────────────▶│ Risk engine │─────────▶│ Broker  │
│ screeners  │             │ (tool loop   │             │ 20 rules +  │          │ plugin  │
└────────────┘             │  live data)  │             │ circuit brk │          └─────────┘
      ▲                    └──────┬───────┘             └─────────────┘               │
      │        live quotes/chains/news/calendars              ▲                       │
┌─────┴──────────────────────────────────────────┐            │ sync                  │
│ Data router (Polygon/Finnhub/TwelveData/… with │      ┌─────┴──────┐                │
│ automatic failover + staleness rejection)      │      │ Portfolio  │◀───────────────┘
└────────────────────────────────────────────────┘      └────────────┘
```

## Highlights

- **Claude as portfolio manager** — continuous review cycles over portfolio,
  news, earnings, economic calendar, option chains, volatility, and
  strategy signals; every decision ships with a full explainability report
  (thesis, timing, edge, risk/reward, confidence, exit plan, max loss,
  alternatives).
- **Live data only, enforced in code** — every datum is timestamped and
  graded; stale data is rejected before it can reach the AI or an order.
  If data is unavailable, Poseidon explains why and does not trade.
- **Three operating modes** — research (no orders), approval (human
  confirms each trade on the dashboard), autonomous (execute within risk
  limits).
- **Position guardian** — every entry's stop-loss/take-profit is armed and
  enforced against live quotes between review cycles (alert / propose /
  execute, by mode). Exit plans are binding, not prose.
- **Performance analytics** — Sharpe, Sortino, drawdown, win rate, profit
  factor, expectancy, monthly returns, and per-strategy P&L attribution
  from the platform's own fill history; AI token/cost metering with an
  optional hard monthly budget.
- **Risk-desk metrics** — 1-day historical VaR and expected shortfall
  (95/99%), portfolio beta, most-correlated-pair detection, and annualized
  volatility, recomputed from live bar history every 15 minutes and
  available to the AI as a tool; optionally enforced as a hard VaR limit
  on new risk.
- **Regime-aware, risk-equalized** — a live market-regime read (trend,
  vol percentile, drawdown → risk-on/neutral/risk-off/stress) feeds every
  review cycle, and a vol-targeted sizing tool gives the AI equal-risk
  share counts instead of round numbers.
- **You trade too** — a manual trade ticket places your own orders
  through the identical pipeline (every risk rule, preflight, TCA,
  audit); Claude sees your fills next cycle and manages around them.
- **Algorithm workshop** — write custom screeners in the dashboard, have
  Claude author them during cycles (always saved as drafts for your
  approval), or paste algorithms from other platforms (Pine Script,
  thinkScript, ...) for Claude to review and convert. Activate, edit,
  archive — validated, hot-reloaded, audited.
- **Professional dashboard** — a sidebar-navigated dark UI (Overview,
  Portfolio, AI Desk, Risk, Performance, System) with live tiles, equity
  curve, approvals with one-click actions, toasts, and the full audit
  trail. No frameworks, no CDNs — self-contained and localhost-only.
- **Execution quality (TCA)** — arrival price captured at risk validation,
  signed slippage in bps on every fill, and a standing best-execution
  report (fill rate, per-side/per-symbol cost, time-to-fill).
- **Institutional risk engine** — position/exposure/leverage caps, daily/
  weekly loss limits, drawdown halt, options exposure caps, hard sector
  concentration (live taxonomy), portfolio VaR halt, liquidity and
  spread filters, slippage bands, event blackouts, per-symbol cooldowns,
  order-rate limits, duplicate prevention, buying-power verification,
  broker-side preflight (Public.com), and an error-rate circuit breaker.
  Every order passes every rule.
- **Broker plugins** — Alpaca, Tradier, tastytrade, Schwab, Interactive
  Brokers (Client Portal), Public.com (stocks/options/crypto, free API),
  and a live-quote paper broker. Brokers without official APIs (Fidelity,
  M1, Robinhood equities, …) ship as documented stubs — no terms-violating
  automation, ever. Add a broker by implementing one class
  ([docs/plugin-development.md](docs/plugin-development.md)).
- **Market data failover** — Public.com, Finnhub, Twelve Data, Alpha
  Vantage, Alpaca, Tradier, Polygon, tried in priority order with
  penalty-box backoff.
- **Runs on $0 of subscriptions** — every supported data provider has a
  free tier, and the Public.com API (trading + real-time quotes, bars,
  option chains with greeks) is free with a brokerage account. The only
  required spend is your Anthropic API usage
  ([docs/api-configuration.md](docs/api-configuration.md)).
- **Security** — scrypt+Fernet encrypted credential vault, hash-chained
  tamper-evident audit log (verified nightly and at startup), secret
  redaction in logs, localhost-only dashboard.
- **Operations** — systemd service with watchdog, health monitor, crash
  recovery (orders and baselines resume), auto-reconnect, notifications
  (desktop/email/Discord/Telegram/webhooks), git-channel self-update.
- **Testing** — 197 unit/integration tests, paper trading, historical
  replay backtester (anti-lookahead), Monte Carlo, walk-forward, and
  crisis stress scenarios.

## Quick start (CachyOS / Arch)

```bash
git clone https://github.com/St3althWarri0r/Aegis-Trader
cd Aegis-Trader
./install.sh                     # one command: venv, config, service, doctor

poseidon vault init                 # create the encrypted credential vault
poseidon vault set anthropic_api_key
poseidon vault set finnhub_api_key  # + any other providers you enable
poseidon config validate
poseidon run                        # dashboard at http://127.0.0.1:8321
```

Native package: `cd packaging && makepkg -si`. Docker: `docker compose -f
docker/docker-compose.yml up -d`.

The default configuration starts in **research mode with the paper
broker** — nothing can trade until you deliberately change both.

## Documentation

| Doc | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | System design, data flow, safety invariants |
| [docs/installation.md](docs/installation.md) | Installer, PKGBUILD, Docker, systemd, updates |
| [docs/configuration.md](docs/configuration.md) | Every configuration field |
| [docs/broker-setup.md](docs/broker-setup.md) | Per-broker setup + API status matrix |
| [docs/api-configuration.md](docs/api-configuration.md) | Data providers & Anthropic API |
| [docs/risk-controls.md](docs/risk-controls.md) | Every risk rule and its rationale |
| [docs/security.md](docs/security.md) | Vault, audit chain, threat model |
| [docs/user-guide.md](docs/user-guide.md) | Modes, dashboard, approvals, daily operation |
| [docs/developer-guide.md](docs/developer-guide.md) | Codebase tour, testing, conventions |
| [docs/plugin-development.md](docs/plugin-development.md) | Writing broker/provider/strategy plugins |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Diagnostics and common failures |

## Disclaimer

This software can place real trades with real money. Trading involves
substantial risk of loss. It is built for the operator's private personal
use; nothing here is financial advice. Read
[docs/risk-controls.md](docs/risk-controls.md) and run in research and
paper modes until you trust the behavior.
