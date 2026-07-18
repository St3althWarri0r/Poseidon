# Risk controls

Every order — proposed by the AI or replayed after approval — passes
through **all** of these rules; there is no fast path. Rules read live
data gathered at validation time; if that data cannot be fetched, the
order is rejected (`no data, no trade`). A rule breach rejects the order,
publishes a `risk.violation` event (dashboard + notification), and is
audited.

## Pre-trade rules

| Rule | Config | What it blocks |
| --- | --- | --- |
| `fresh_portfolio_state` | fixed 120 s | trading against a portfolio snapshot older than 2 minutes (e.g. broker sync down) |
| `market_session` | — | orders outside the regular session (unless the order requests extended hours during pre/after) — also covers holidays, half-days, unknown calendar years, and treats exchange-closed states as CLOSED |
| `max_daily_loss` | `max_daily_loss_pct` | new risk once the day's loss from the day-start baseline reaches the limit (exits stay allowed — see exemptions below) |
| `max_weekly_loss` | `max_weekly_loss_pct` | weekly analogue (ISO-week baseline) |
| `max_drawdown` | `max_drawdown_pct` | trading once equity has fallen this far from its all-time peak (peak persists across restarts) |
| `max_orders_per_day` | `max_orders_per_day` | runaway order loops; the counter resets at midnight ET |
| `trade_cooldown` | `trade_cooldown_seconds` | re-entering the same symbol within the cooldown (exits exempt) |
| `order_notional_bounds` | `max_order_notional`, `min_order_notional` | fat-finger sizes and dust orders |
| `buying_power` | — | buys exceeding live buying power (options buying power for options) |
| `max_position_size` | `max_position_pct` | a single position (existing value + this order) above a % of equity |
| `max_portfolio_exposure` | `max_portfolio_exposure_pct` | gross exposure above a % of equity |
| `max_leverage` | `max_leverage` | gross/equity above the leverage cap |
| `max_options_exposure` | `max_options_exposure_pct` | total option market value above a % of equity |
| `max_sector_concentration` | `max_sector_concentration_pct` | equity buys that would push one sector's exposure above a % of equity (live taxonomy, week-cached — see below) |
| `max_portfolio_var` | `max_portfolio_var_pct` | **optional** — new risk while the book's 1-day historical VaR(95) exceeds a % of equity; enabling it makes fresh risk metrics a *requirement* for new risk (0 = off) |
| `max_spread` | `max_spread_pct` | illiquid names with wide bid/ask spreads (or one-sided books) |
| `min_volume` | `min_avg_volume` | names whose 20-day average volume is below the floor; missing history blocks buys |
| `slippage_protection` | `slippage_limit_pct` | limit prices far from the live quote; market orders when the spread exceeds the band |
| `volatility_halt` | `volatility_halt_daily_move_pct` | new entries in a name that has already moved violently today (per-name circuit-breaker analogue) |
| `news_blackout` | `news_blackout_minutes_before_econ` | new entries in the minutes before high-importance economic releases (FOMC, CPI, …) |

**Sector taxonomy.** The concentration rule classifies symbols through
any SECTOR-capable provider (Finnhub's free company profile today);
results are cached for a week, so steady-state enforcement costs zero API
calls. When a symbol *cannot* be classified — ETFs have no single sector,
or no capable provider is configured — the rule passes for that order and
the AI enforces the cap qualitatively (it sees the config value in
`get_risk_status` and full composition in `get_portfolio`). A taxonomy
gap must not halt all trading; it is a filter, not a price.

## Portfolio risk metrics

On a 15-minute market-hours schedule (and on demand via
`GET /api/risk-metrics` or the AI's `get_risk_metrics` tool), Poseidon
computes from live bar history what a risk desk actually watches:

- **1-day historical VaR and expected shortfall** (95%/99%) of the current
  book — historical simulation over ~6 months of joint daily returns using
  actual position weights, so cross-correlations are captured without a
  normality assumption;
- **portfolio beta** to the configured benchmark (`risk.benchmark_symbol`,
  default SPY);
- **the most correlated pair** of holdings — concentration that per-position
  limits cannot see;
- **annualized portfolio volatility**.

Positions without sufficient usable history (options, fresh listings) are
reported as *uncovered*, never estimated. Set `max_portfolio_var_pct` to
make VaR a hard pre-trade limit; while it is enabled, missing or stale
(>1 h) metrics block new risk — an explicit VaR mandate with no current
VaR estimate means no new positions.

## Market regime & vol-targeted sizing

Two advisory inputs that shape the AI's posture without being trade
signals:

- **Regime**: from live benchmark history, a four-state read —
  ``risk_on`` (uptrend, unexceptional vol), ``neutral``, ``risk_off``
  (downtrend and/or elevated vol), ``stress`` (vol extreme or ≥15% index
  drawdown) — built from trend vs. 50/200-day averages, the 20-day
  realized vol's percentile within its own 1-year range, and drawdown
  from the 1-year high. It is injected into every review-cycle prompt
  and shown in the dashboard header. Insufficient history reads
  ``unknown`` and the AI is told nothing.
- **Vol-targeted sizing** (`suggest_position_size` tool): shares such
  that one typical day moves the position by `position_risk_budget_pct`
  of equity — equal risk per position instead of equal notional — capped
  by the position-size limit and live buying power. Advisory: the risk
  engine still validates every order.

### Risk-reducing order exemptions

Halts and entry filters exist to stop *new* risk — they must never trap
the operator in a position. Orders whose side reduces risk (`sell`,
`sell_to_close`, `buy_to_close`) are therefore exempt from the loss-limit
halts (daily/weekly/drawdown), the liquidity entry filters
(spread/volume), and the per-symbol cooldown. They still pass everything
else: session checks, notional bounds, slippage protection, duplicate
prevention, and the circuit breaker. `sell_to_open` (opening short option
risk) is deliberately **not** exempt.

### Asset-class exemptions (crypto)

Crypto orders (`BASE/USD`, e.g. `BTC/USD`) pass the **full** risk engine with
exactly **two** exemptions, both category corrections rather than relaxations:

- **`market_session`** — spot crypto trades 24/7, so the regular-session gate
  does not apply. (`MarketOpenRule` already skips crypto.)
- **`min_volume`** — `min_avg_volume` is a *share*-count floor (default
  100,000). Crypto `Bar.volume` is denominated in *coins*: BTC trades tens of
  thousands of coins/day (~$30B notional) yet would fail a 100k-*share* floor.
  Applying a share count to a coin count is a category error, so the volume
  floor is skipped for crypto — liquidity stays gated by `max_spread` and
  `slippage_protection`, which use spread % and are asset-class-neutral.

**Everything else applies to crypto unchanged:** notional/position/exposure/
leverage caps, buying power, `volatility_halt` (crypto is volatile — kept),
cooldown, orders-per-day, the daily/weekly/drawdown loss halts, VaR, the
economic blackout, the circuit breaker, and `fresh_portfolio_state`. In
particular a whole-BTC buy above `max_order_notional` is *correctly* rejected
(size fractionally). Crypto is **not** exempt from the reduce-only guard — the
platform still never opens a short crypto position.

### Dedicated sleeves

An algorithm can be given a **capital sleeve** (a fraction of equity, set
in the Algorithms editor). Orders attributed to that algorithm use the
sleeve as their per-position cap instead of `max_position_pct`, so a
concentrated rotation model (e.g. a 100%-TQQQ symphony) can run at full
weight *inside its allocation* while the rest of the book keeps the
tighter institutional limit. Sleeves substitute exactly one rule — the
position-size cap — and nothing else: gross exposure, leverage, loss
halts, liquidity filters, VaR, blackouts, and the circuit breaker apply
to sleeve orders unchanged. Sleeves take effect only while the algorithm
is active, and every change is audited.

## Position guardian

Every AI entry carries an exit plan; the guardian makes it binding. When
an entry order fills, the decision's numeric stop-loss / take-profit is
persisted ("armed") for that symbol. On a short interval during market
hours (default 60 s), each armed plan is checked against a live,
freshness-graded quote:

- **breach in research mode** → warning notification, no order;
- **approval mode** → an exit order is proposed into the normal approval
  queue with a rationale explaining which level was hit;
- **autonomous mode** → the exit executes through the order manager
  (still passing the risk engine), as a limit order at the live bid.

Triggered plans latch (no re-fire loops); plans deactivate automatically
when the position is closed by any path. Free-text ``time_stop`` entries
("exit before earnings") are not machine-enforced — the AI handles those
during review cycles, and the dashboard shows them so you can too. Every
trigger is audited and notified.

## Circuit breaker & cooldowns

- **Error-rate breaker**: `circuit_breaker_error_threshold` execution-path
  errors within `circuit_breaker_window_seconds` opens the breaker for
  `circuit_breaker_cooldown_seconds`. While open, every order is refused
  and a critical notification is sent.
- **Manual halt**: the dashboard HALT button (or `POST /api/halt`)
  force-opens the breaker until you resume — the kill switch.
- **Audit-integrity halt**: a failed nightly audit-chain verification
  force-opens the breaker.
- **Loss-limit halts**: daily/weekly/drawdown rules act as latched halts —
  they clear only when the corresponding baseline rolls.
- **Per-symbol cooldowns** prevent rapid-fire re-trading.

## Order-level protections

- **Broker preflight** (where the broker supports it — Public.com today):
  after the risk engine passes and immediately before submission, the
  broker's own preflight endpoint validates buying power, margin impact,
  and short locate against live account state. A definitive broker
  rejection stops the order with the broker's reason; an *unavailable*
  preflight never vetoes (the submission itself remains the authoritative
  check).
- **Duplicate prevention**: unique client order IDs persisted before
  submission and passed to the broker; plus a live check for an identical
  open order at the broker. If open orders cannot be verified, the order
  is refused.
- **Post-approval re-validation** (Mode 2): after the human approves, the
  full rule set runs again against fresh data; approvals expire after 15
  minutes.
- **Bounded retries**: submissions retry up to 3 times on retryable broker
  errors with exponential backoff, then fail loudly. Non-retryable errors
  never retry.
- **Lifecycle polling**: every submitted order is polled to a terminal
  state; fills and rejections are audited and notified.

## Execution quality (TCA)

Every order records its **arrival price** — the live mid at the moment it
passed final risk validation. On fill, Poseidon computes signed
implementation shortfall in basis points (positive always = cost: paid
more on a buy, received less on a sell). `GET /api/execution` aggregates
fill rate, average/median/worst slippage, per-side and per-symbol cost,
and average time-to-fill — a standing best-execution review built from
the platform's own records, not broker marketing.

## Structural safeguards

- The AI cannot place orders directly — only `submit_decision`, which
  flows through the order manager and risk engine.
- Trades without a complete rationale (thesis, timing, risk, reward,
  confidence, exit plan, max loss, alternatives) are voided at parse time.
- Research mode is the config default; autonomous mode must be enabled
  deliberately and is highlighted amber in the dashboard.
- Every consequential action lands in the tamper-evident audit log.
