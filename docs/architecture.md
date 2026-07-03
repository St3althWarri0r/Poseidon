# Architecture

## Design goals

1. **Never fabricate market data.** Every price, greek, spread, calendar
   entry, and headline the AI sees comes from a live provider call made in
   the same review cycle, timestamped and freshness-graded.
2. **No path around the risk engine.** The order manager owns the only
   broker reference used for submission; the risk engine runs on every
   order in every mode.
3. **Fail safe, not silent.** Missing data, stale state, unknown calendar
   years, broken audit chains — all halt or degrade loudly, never guess.
4. **Everything auditable.** Decisions, approvals, submissions, rejections,
   mode changes, and halts land in a hash-chained audit log.

## Layers

```
src/poseidon/
├── core/          domain models, enums, errors, config, event bus, market clock, DI
├── security/      encrypted vault, tamper-evident audit log
├── storage/       async SQLite (WAL): orders, decisions, equity, audit, kv
├── data/          provider ABC, 6 providers, failover router (staleness gate)
├── brokers/       broker ABC, plugin registry, 6 live plugins + 6 documented stubs
├── portfolio/     portfolio state + continuous sync service
├── risk/          20 pre-trade rules, circuit breaker, cooldowns
├── execution/     order manager (the only broker path), approval queue
├── ai/            Claude agent (tool loop), tool dispatcher, schemas, reports
├── strategy/      strategy ABC + 16 built-in screeners + engine
├── scheduler/     interval + cron jobs, overlap protection
├── notifications/ desktop/email/discord/telegram/webhook channels + router
├── health/        probes, watchdog (sd_notify), self-diagnostics
├── backtest/      replay engine, Monte Carlo, walk-forward, stress
├── api/           FastAPI dashboard (REST + websocket + static dark UI)
├── app.py         ApplicationKernel — composition root
└── cli.py         poseidon command
```

## The review cycle

Triggered by the scheduler (default: every `ai.review_interval_seconds`
during market hours, plus any custom cron schedules) or manually:

1. **Strategy scan.** Every enabled strategy inspects live data for its
   symbols and emits signals ("NVDA momentum long: +8.4% 20d, above 50d MA,
   volume 1.6× average"). Strategies are screeners — they cannot place
   orders.
2. **Agent run.** Claude receives the cycle context (mode, watchlist,
   enabled strategies, signals, session) and a fixed, cache-controlled
   system prompt. It calls data tools (`get_quote`, `get_bars`,
   `get_option_chain`, `get_news`, `get_earnings_calendar`,
   `get_economic_calendar`, `get_portfolio`, `get_risk_status`) — each
   backed by the failover router — then calls `submit_decision` exactly
   once. The decision schema is strict; trades without a complete
   rationale are voided in parsing.
3. **Persist + report.** The decision (with rationale and the list of data
   sources actually used) is stored, audited, published to the dashboard,
   and rendered into the notification report.
4. **Execution.** For each proposed trade the order manager:
   - refuses outright in research mode;
   - runs all 18 risk rules against a *fresh* quote (any data failure
     blocks the order);
   - in approval mode, queues for the human with a 15-minute TTL and
     re-runs the risk rules after approval;
   - checks for duplicates (client-order-id history + identical open
     orders at the broker);
   - submits with bounded retries and polls the order to a terminal state.

## Data freshness model

Providers return models carrying `as_of` (their own timestamps where the
API provides them) and `source`. `FreshnessPolicy` grades age:
REAL_TIME (≤5 s), DELAYED (≤15 min), STALE. The router:

- rejects STALE always;
- rejects DELAYED for anything feeding an order (risk-engine quotes);
- optionally admits DELAYED for research context
  (`data.allow_delayed_for_research`).

Naive (timezone-less) timestamps are graded STALE on principle.

## Failover

The router sorts providers by priority per capability. A failing provider
enters a penalty box with exponential backoff (15 s → 10 min); penalized
providers are skipped and retried only as a last resort. When every capable
provider fails, `AllProvidersFailedError` propagates and the cycle/order is
abandoned — the "no data, no trade" invariant.

## Crash recovery

- Orders open at the broker are re-attached to status pollers at startup
  (`resume_open_orders`), keyed by persisted client order IDs.
- Day/week equity baselines and peak equity reload from SQLite.
- The paper broker persists its simulated account to disk.
- The audit chain is verified at startup; a broken chain refuses to boot.
- systemd restarts the process on failure; the watchdog restarts it on hang.

## Event bus

An in-process async pub/sub bus decouples subsystems. The notifier,
dashboard websocket, and audit observers subscribe; publishers never block
on subscribers, and one failing handler cannot affect another.

## Concurrency model

Single asyncio event loop. Long CPU work does not exist in the hot path;
SMTP (blocking) is pushed to a thread. The review cycle takes a lock so
overlapping triggers are skipped, and the scheduler additionally protects
each job from overlapping itself.
