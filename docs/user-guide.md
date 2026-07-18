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

A sidebar-navigated app with eight views. The header is always present:
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
- **AI Desk** — **chat with Claude** (see *Chatting with Claude*); pending
  approvals with thesis/confidence/max-loss and countdown; the full
  reasoning log; AI token usage and estimated spend (chat included).
- **Risk** — 1-day VaR/expected shortfall, portfolio beta, annualized
  vol, most-correlated pair, the market-regime read (trend, vol
  percentile, drawdown), loss-limit meters, and metric coverage.
- **Performance** — portfolio and trade statistics, per-strategy
  attribution, monthly returns, and the execution-quality (TCA) report.
- **Algorithms** — the workshop: your saved custom screeners (drafts,
  active, archived), an editor, and the Claude import/review flow (see
  *The algorithm workshop*).
- **Account** — the active broker (paper/LIVE badge, equity, sync state)
  and the brokerage connector (see *Connecting your brokerage*).
- **System** — component health, data-provider latency/penalty status,
  scheduler runtime, and the tamper-evident audit trail.

## Connecting your brokerage

The Account view connects a real brokerage without touching a terminal:

1. Pick a broker tile (Public, Alpaca, Tradier, tastytrade, Schwab, IBKR —
   the paper simulator is a tile too, so you can always switch back).
   A tile marked *fees may apply* shows a cost note in its form (the
   platform itself is always free; some brokers bill for extras like IBKR
   market-data subscriptions).
2. Enter the credentials the form asks for. They are written **only to the
   encrypted vault**, never to a file; a saved credential can be reused by
   leaving the fields blank.
3. **Test connection** proves auth and shows the account (id, equity,
   buying power) without changing anything.
4. **Connect & switch** re-proves the connection, stores the credential,
   persists the choice (`poseidon.local.yaml` — safe to delete to revert),
   and hot-swaps the active broker. No restart needed. **Sync now** pulls
   the account fresh at any time.

The paper tile takes a **Starting cash** amount — entering one resets the
simulator to a fresh account at that balance (with a confirmation).

Safety rails: switching never changes the operating mode (a LIVE account in
research mode still cannot trade); the switch is refused while any order is
open; and equity history, loss baselines, and peak-drawdown tracking are
per-broker-and-environment, so a paper history can never trip a halt on
your real account. Brokers without an official self-service API (Fidelity,
Vanguard, M1, Robinhood equities, …) cannot be connected at any price —
there is no compliant interface to them; see docs/broker-setup.md's status
matrix.

## Auto-investing an algorithm

Every algorithm card in the workshop has **▶ Start auto-investing**: it
activates the algorithm *and* switches Poseidon to autonomous mode (with a
confirmation spelling out exactly what that means), so Claude executes the
algorithm's signals within every risk limit from the next cycle on. The
status line under the editor always states plainly whether an algorithm is
auto-investing, feeding approvals, or signals-only. Prefer to confirm each
trade yourself? *Activate (signals only)* + Approval mode gives you a
one-click approval queue instead. *Stop / deactivate* ends it.

## Paper dry run

Before trading real money, run the whole autonomous stack — Claude *and* the
built-in algorithms — against the **paper** account, risk-free. Open the
**Dry Run** view in the left nav and turn on its three transparent steps:
**Paper broker**, **built-in algorithms active**, and **Autonomous mode**. A
persistent "PAPER — no real money" banner makes clear it is a safe simulation,
and a market-open indicator tells you whether trades can fill yet. Use **Run a
review cycle now** to trigger Claude on demand instead of waiting for the
scheduler (trades only fill during market hours). **Stop dry run** returns the
platform to Research mode. Nothing on this view can touch a live account — it
only ever engages autonomous mode on the paper simulator.

## The desktop app

`poseidon app` (also the **Poseidon** entry in your application menu) opens
the dashboard as its own desktop window — no tabs, no URL bar. It uses a
native window when `pip install poseidon[gui]` (pywebview) is installed,
otherwise a Chromium app-mode window. The engine itself stays a background
service: closing the window never stops trading. If the engine isn't
running, `poseidon app` starts the systemd service when it can, or tells
you exactly what to run.

## Chatting with Claude

The AI Desk's chat panel talks to the same Claude that runs review cycles,
with the same live-data tools — quotes, bars, chains, news, your portfolio,
risk metrics, performance, backtests. The same honesty rules apply: it
fetches live numbers rather than reciting from memory, and says so when data
is unavailable. The chat **cannot place, modify, or cancel orders** — for
trading it will point you to the trade ticket or the operating modes. It can
draft algorithms into the workshop on request (drafts only; you activate).
History survives restarts; *Clear* wipes it. Chat tokens are metered into
the same monthly budget as review cycles.

## Trading manually

Claude managing the book never locks you out of it. The Portfolio view's
trade ticket places your own orders — equities, market/limit/stop, day or
GTC, extended hours — with a live freshness-graded quote beside the form.
Manual orders take the exact pipeline AI orders take: **every risk rule**,
duplicate prevention, broker preflight, lifecycle polling, TCA slippage
capture, and the audit log (actor: `human`).

**Crypto.** Enter a spot crypto pair with a slash — `BASE/USD`, e.g.
`BTC/USD` or `ETH/USD` — and the ticket handles the rest: the symbol is
auto-tagged as crypto (no asset-class picker needed), quoted 24/7, and
fractional sizes (e.g. `0.05 BTC`) are allowed. Crypto orders clear the full
risk engine with only two exemptions — the market-hours gate (crypto trades
around the clock) and the equity share-count volume floor; see
docs/risk-controls.md. Crypto is **paper-only** today and needs a crypto-capable
data provider configured (enable `alpaca` — see docs/api-configuration.md);
only `BASE/USD` pairs are supported (a stablecoin pair like `BTC/USDT` is
rejected with a clear message).

Two deliberate differences:
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

Rotation models (Composer symphonies, tactical allocation trees) port
naturally: compute the target book and emit one `long` signal per holding
with `target_weight` in the evidence, plus `exit` signals for holdings
that fell out — in autonomous mode the AI executes the rebalance through
the risk engine with no human input. A full indicator suite is
built in via `ctx.ta` — every Composer function (`rsi` (Wilder), `sma`,
`ema`, `cumulative_return`, `moving_average_return`, `stdev_price`,
`stdev_return`, `max_drawdown`, percent units throughout) plus the
standard desk set (`macd`, `bollinger`, `stochastic`, `atr`, `adx`,
`obv`, `rate_of_change`, `highest`/`lowest`), with the four most common
also directly on ctx (`ctx.rsi`, `ctx.sma`, ...). Every function returns
None on insufficient history — never a guess. Use **Test run** in the
editor to dry-run any saved algorithm against live data, and
**Backtest 5y** to replay it through real historical daily bars with the
anti-lookahead window — target-weight rebalancing, slippage, annual
returns, Sharpe, drawdown — before activating. Give a concentrated
rotation model a **sleeve** (% of equity) and its orders may use that
allocation as their position cap while every other risk rule still
applies (docs/risk-controls.md#dedicated-sleeves). On first boot the library is
pre-seeded (as drafts) with four bundled algorithms: faithful ports of
the operator's three Composer symphonies under their original names, and
**TQQQ Day Trader** — an intraday 5-minute mean-reversion sibling that
re-evaluates every review cycle, entering as many times per day as its
setup appears (zero on quiet days) and flattening into the close. For
intraday algorithms, set a short `ai.review_interval_seconds` (60–300s)
and give the algorithm a sleeve.

A word on trust: algorithms run in-process, like installed plugins. The
static screen is a guardrail against accidents, not a sandbox — read
anything you paste from the internet before activating it.

## Small accounts (starting with ~$100)

A deliberately small first deposit works, with three adjustments:

- Use a **fractional-shares broker** (Public.com or Alpaca). With a 10%
  position cap a $100 account trades $10 slices — impossible in whole
  shares of most ETFs.
- Leave `min_order_notional` at its $1 default and consider lowering
  `max_orders_per_day`; more importantly, know that **PDT rules** allow
  only 3 day trades per rolling 5 sessions in a margin account under
  $25k (cash accounts are exempt but settle T+1). The intraday
  algorithm is the one this constrains.
- Expect metrics noise: at $100, commissions/slippage of cents are
  whole percentage points. Treat the run as a *behavioral* test — did
  the platform do exactly what it said, with a perfect audit trail —
  not a returns test.

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
