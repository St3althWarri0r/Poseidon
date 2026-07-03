# Aegis Trader

Autonomous AI trading platform for private, single-user operation on Linux.
Claude is the portfolio manager; every market fact it reasons about comes
from live, authoritative data providers — never from memory, never estimated.

```
┌────────────┐   signals   ┌──────────────┐  decisions  ┌─────────────┐  orders  ┌─────────┐
│ Strategy   │────────────▶│ Claude agent │────────────▶│ Risk engine │─────────▶│ Broker  │
│ screeners  │             │ (tool loop   │             │ 18 rules +  │          │ plugin  │
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
  If data is unavailable, Aegis explains why and does not trade.
- **Three operating modes** — research (no orders), approval (human
  confirms each trade on the dashboard), autonomous (execute within risk
  limits).
- **Institutional risk engine** — position/exposure/leverage caps, daily/
  weekly loss limits, drawdown halt, options exposure caps, liquidity and
  spread filters, slippage bands, event blackouts, per-symbol cooldowns,
  order-rate limits, duplicate prevention, buying-power verification, and
  an error-rate circuit breaker. Every order passes every rule.
- **Broker plugins** — Alpaca, Tradier, tastytrade, Schwab, Interactive
  Brokers (Client Portal), and a live-quote paper broker. Brokers without
  official APIs (Fidelity, M1, Robinhood equities, …) ship as documented
  stubs — no terms-violating automation, ever. Add a broker by
  implementing one class ([docs/plugin-development.md](docs/plugin-development.md)).
- **Market data failover** — Polygon, Finnhub, Twelve Data, Alpha Vantage,
  Alpaca, Tradier, tried in priority order with penalty-box backoff.
- **Security** — scrypt+Fernet encrypted credential vault, hash-chained
  tamper-evident audit log (verified nightly and at startup), secret
  redaction in logs, localhost-only dashboard.
- **Operations** — systemd service with watchdog, health monitor, crash
  recovery (orders and baselines resume), auto-reconnect, notifications
  (desktop/email/Discord/Telegram/webhooks), git-channel self-update.
- **Testing** — 81 unit/integration tests, paper trading, historical
  replay backtester (anti-lookahead), Monte Carlo, walk-forward, and
  crisis stress scenarios.

## Quick start (CachyOS / Arch)

```bash
git clone https://github.com/St3althWarri0r/Aegis-Trader
cd Aegis-Trader
./install.sh                     # one command: venv, config, service, doctor

aegis vault init                 # create the encrypted credential vault
aegis vault set anthropic_api_key
aegis vault set polygon_api_key  # + any other providers you enable
aegis config validate
aegis run                        # dashboard at http://127.0.0.1:8321
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
