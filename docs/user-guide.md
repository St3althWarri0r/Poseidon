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

A sidebar-navigated app with six views. The header is always present:
mode switch (Research / Approval / Auto), market session, **market
regime**, health, circuit state, *Run cycle*, and **HALT** (kill switch —
opens the circuit breaker until *Resume*; asks for confirmation). Action
feedback arrives as toasts; the AI Desk item shows a badge while trades
await your approval.

- **Overview** — equity, day P&L, cash/buying power, drawdown, exposure,
  and regime tiles; the equity curve with crosshair inspection; loss-limit
  meters; allocation; and the live event feed.
- **Portfolio** — positions (with portfolio weight), the **trade ticket**
  (your own orders — see *Trading manually*), armed guardian exit plans,
  and the order blotter (fills show their slippage; open orders can be
  canceled).
- **AI Desk** — pending approvals with thesis/confidence/max-loss and
  countdown; the full reasoning log; AI token usage and estimated spend.
- **Risk** — 1-day VaR/expected shortfall, portfolio beta, annualized
  vol, most-correlated pair, the market-regime read (trend, vol
  percentile, drawdown), loss-limit meters, and metric coverage.
- **Performance** — portfolio and trade statistics, per-strategy
  attribution, monthly returns, and the execution-quality (TCA) report.
- **Algorithms** — the workshop: your saved custom screeners (drafts,
  active, archived), an editor, and the Claude import/review flow (see
  *The algorithm workshop*).
- **System** — component health, data-provider latency/penalty status,
  scheduler runtime, and the tamper-evident audit trail.

## Trading manually

Claude managing the book never locks you out of it. The Portfolio view's
trade ticket places your own orders — equities, market/limit/stop, day or
GTC, extended hours — with a live freshness-graded quote beside the form.
Manual orders take the exact pipeline AI orders take: **every risk rule**,
duplicate prevention, broker preflight, lifecycle polling, TCA slippage
capture, and the audit log (actor: `human`). Two deliberate differences:
there is no approval queue (you are the approver), and research mode still
refuses — research means no orders from anyone. Your fills sync into the
portfolio like any others, so Claude sees and manages around them on the
next cycle.

## The algorithm workshop

Custom screeners, written by you or by Claude, saved in the platform:

- **Write** one in the Algorithms editor. The contract is a single
  function — ``async def scan(ctx)`` — with live data (``ctx.quote``,
  ``ctx.bars``, ``ctx.option_chain``), your watchlist, params, and a
  read-only portfolio view. It returns signal rows (symbol, direction,
  strength, evidence). Algorithms are screeners with the same standing as
  the 16 built-ins: their signals feed Claude's review cycle — they can
  never place orders directly.
- **Import** one from anywhere: paste Pine Script, thinkScript,
  QuantConnect Python, or pseudocode into *Import & Claude review*, add
  instructions if you like, and Claude analyzes it (flagging lookahead
  bias, overfit parameters, features that don't translate) and produces a
  ready-to-save Poseidon implementation when the idea works as a screener
  — or tells you honestly when it doesn't.
- **Claude can author them too**: during review cycles the agent can save
  an algorithm it considers worth running (`propose_algorithm`). Anything
  Claude writes lands as a **draft** — only you can activate.
- **Lifecycle**: drafts are validated on save (syntax, the scan contract,
  and a static screen that rejects file/network/os access); *Activate*
  compiles it into the live engine immediately; edits to an active
  algorithm hot-reload; *Deactivate* or archive any time. Every state
  change is audited. A broken active algorithm demotes itself to draft at
  startup instead of blocking the platform.

A word on trust: algorithms run in-process, like installed plugins. The
static screen is a guardrail against accidents, not a sandbox — read
anything you paste from the internet before activating it.

## Approvals (Mode 2)

A proposed trade generates a warning-level notification and appears on the
dashboard with its full report. You have 15 minutes; expiry counts as
rejection. After you approve, the risk engine re-validates against fresh
data before submission — an approval cannot execute in a market that has
moved outside the limits.

## When Poseidon refuses to trade

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
poseidon run                 start (foreground)
poseidon cycle               one review cycle, then exit
poseidon doctor              self-diagnostics
poseidon config validate     check the YAML
poseidon vault set NAME      store/replace a credential
poseidon audit tail -n 50    recent audit records
poseidon audit verify        verify the tamper-evident chain
poseidon update check|apply  self-update (git installs)
```

## Backtesting & simulation

The backtester replays the same strategy screeners over historical daily
bars with anti-lookahead visibility, next-close execution with slippage,
stops/targets/time exits, and reports return, drawdown, Sharpe, and win
rate; Monte Carlo, walk-forward, and crisis stress analyses build on the
result (see `poseidon.backtest` and docs/developer-guide.md for
programmatic use). The AI judgment layer is *not* simulated — historical
news and calendars don't exist to feed it honestly. Evaluate the full loop
forward-in-time with the paper broker instead.
