# User guide

## Operating modes

| Mode | Who decides | Who executes | Use it for |
| --- | --- | --- | --- |
| 1 · `research` | Claude analyzes and writes decisions | nobody — orders are never submitted | learning the system, tuning strategies, watching reasoning quality |
| 2 · `approval` | Claude proposes trades | you, per trade, on the dashboard | production with a human gate |
| 3 · `autonomous` | Claude | Claude, inside the risk limits | full autonomy once trust is earned |

Switch modes from the dashboard header or `POST /api/mode`. Every change
is audited. Recommended ramp: research (days) → approval on the paper
broker → approval on a live broker with small limits → autonomous, if
ever.

## A day in the life

1. The scheduler fires review cycles (default: every 5 minutes during
   market hours; plus your cron schedules, e.g. a 17:30 post-close review).
2. Each cycle: strategy screeners scan live data → Claude reviews the
   portfolio, positions vs. their exit plans, news, earnings, the economic
   calendar, volatility, and the signals → it submits one decision with a
   complete rationale.
3. The risk engine validates any proposed trades against live quotes.
4. Depending on mode, trades stop (research), wait for you (approval, 15
   minute expiry), or execute (autonomous). Fills, rejections, and risk
   violations reach you through your configured notification channels.
5. The portfolio syncs continuously (balances, positions, orders, fills,
   tax lots, dividends); equity marks feed the drawdown and loss limits.

Outside market hours the default review schedule pauses (cron schedules
you define run whenever you set them); the sync, health monitor, and
dashboard run 24/7.

## The position guardian

Exit plans are not suggestions. When an entry fills, its stop-loss /
take-profit is armed; the guardian checks armed levels against live quotes
every minute during market hours and acts by mode — alert (research),
propose (approval), execute (autonomous). Armed plans are visible on the
dashboard's *Exit plans* card, and every trigger notifies you. See
docs/risk-controls.md#position-guardian.

## Performance & cost

The *Performance* card (and `GET /api/performance`) reports total return,
CAGR, Sharpe/Sortino, max drawdown, volatility, win rate, profit factor,
expectancy, and realized P&L attributed per strategy — all computed from
the platform's own recorded fills and equity marks. The *AI usage* card
meters review cycles, tokens, and estimated monthly spend; set
`ai.monthly_budget_usd` for a hard ceiling (cycles pause at the cap and
you're notified). A daily digest lands on your notification channels at
16:15 ET (configurable under `reports`).

## The dashboard (http://127.0.0.1:8321)

- **Header**: mode selector, market session, overall health, circuit
  state, *Run cycle* (manual review), **HALT** (kill switch — opens the
  circuit breaker until *Resume*).
- **Tiles**: equity, day P&L, cash/buying power, drawdown, gross/options
  exposure.
- **Equity chart** with crosshair inspection.
- **Risk limits**: live meters for daily/weekly loss and drawdown against
  their limits, plus the order budget for the day.
- **Pending approvals** (Mode 2): the proposal with thesis, confidence,
  max loss, and countdown; Approve/Reject buttons.
- **Positions / Orders**: live tables; open orders can be canceled.
- **AI reasoning log**: every decision with its rationale and the data
  sources actually used.
- **System / Data providers**: component health, broker connection,
  provider latency and penalty status.
- **Event feed**: the live event bus (fills, syncs, violations, health
  transitions).

## Approvals (Mode 2)

A proposed trade generates a warning-level notification and appears on the
dashboard with its full report. You have 15 minutes; expiry counts as
rejection. After you approve, the risk engine re-validates against fresh
data before submission — an approval cannot execute in a market that has
moved outside the limits.

## When Aegis refuses to trade

By design you will see cycles end with *no action* and reasons such as:

- required data unavailable (all providers failed / stale) — listed in the
  decision's `data_gaps`;
- risk rule violations (each names the rule and the numbers);
- circuit breaker open (error burst, manual halt, or audit failure);
- loss limit reached for the day/week or drawdown cap hit;
- market closed / holiday / economic-release blackout.

These are the platform working, not failing. Each is visible in the
reasoning log, the event feed, and notifications.

## CLI quick reference

```
aegis run                 start (foreground)
aegis cycle               one review cycle, then exit
aegis doctor              self-diagnostics
aegis config validate     check the YAML
aegis vault set NAME      store/replace a credential
aegis audit tail -n 50    recent audit records
aegis audit verify        verify the tamper-evident chain
aegis update check|apply  self-update (git installs)
```

## Backtesting & simulation

The backtester replays the same strategy screeners over historical daily
bars with anti-lookahead visibility, next-close execution with slippage,
stops/targets/time exits, and reports return, drawdown, Sharpe, and win
rate; Monte Carlo, walk-forward, and crisis stress analyses build on the
result (see `aegis_trader.backtest` and docs/developer-guide.md for
programmatic use). The AI judgment layer is *not* simulated — historical
news and calendars don't exist to feed it honestly. Evaluate the full loop
forward-in-time with the paper broker instead.
