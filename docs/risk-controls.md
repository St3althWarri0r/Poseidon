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
| `max_spread` | `max_spread_pct` | illiquid names with wide bid/ask spreads (or one-sided books) |
| `min_volume` | `min_avg_volume` | names whose 20-day average volume is below the floor; missing history blocks buys |
| `slippage_protection` | `slippage_limit_pct` | limit prices far from the live quote; market orders when the spread exceeds the band |
| `volatility_halt` | `volatility_halt_daily_move_pct` | new entries in a name that has already moved violently today (per-name circuit-breaker analogue) |
| `news_blackout` | `news_blackout_minutes_before_econ` | new entries in the minutes before high-importance economic releases (FOMC, CPI, …) |

`max_sector_concentration_pct` is enforced qualitatively by the AI (it has
the config value in `get_risk_status` and portfolio composition in
`get_portfolio`) — a data provider for sector taxonomies can make it a
hard rule via a custom `RiskRule` (docs/plugin-development.md).

### Risk-reducing order exemptions

Halts and entry filters exist to stop *new* risk — they must never trap
the operator in a position. Orders whose side reduces risk (`sell`,
`sell_to_close`, `buy_to_close`) are therefore exempt from the loss-limit
halts (daily/weekly/drawdown), the liquidity entry filters
(spread/volume), and the per-symbol cooldown. They still pass everything
else: session checks, notional bounds, slippage protection, duplicate
prevention, and the circuit breaker. `sell_to_open` (opening short option
risk) is deliberately **not** exempt.

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

## Structural safeguards

- The AI cannot place orders directly — only `submit_decision`, which
  flows through the order manager and risk engine.
- Trades without a complete rationale (thesis, timing, risk, reward,
  confidence, exit plan, max loss, alternatives) are voided at parse time.
- Research mode is the config default; autonomous mode must be enabled
  deliberately and is highlighted amber in the dashboard.
- Every consequential action lands in the tamper-evident audit log.
