# CLAUDE.md — `poseidon.risk`

Guidance for Claude Code when working in the risk engine. Supplements the repository-root `CLAUDE.md`; its invariants (one order path, `Decimal` money, audit-append) still apply.

## What this package is

The **mandatory pre-trade gate** between any decision and any order. `engine.py` (`RiskEngine.validate_order`) is the only validation path; `rules.py` holds the individual rules; `circuit.py` holds the error-rate breaker and per-symbol cooldowns. The order manager owns the sole `Broker.submit_order` call and invokes `validate_order` first — there is deliberately no way to submit an order that skips it.

`validate_order(order) -> Quote` gathers **live** inputs (a fresh quote with `allow_delayed=False`, 30d daily bars, the economic calendar, sector classifications), builds a `RiskContext`, and runs **every** rule in `ALL_RULES` — no fast path skips checks. It raises `RiskViolation` (a rule breached, published on `Topics.RISK_VIOLATION`), `CircuitBreakerOpen`, or `DataError` (the live context couldn't be assembled). A `DataError` here means the order must not go out: **no data, no trade** — there are deliberately no fallbacks to cached or assumed values.

## Invariants — do not break these

1. **The risk-reducing exemption depends on `ReduceOnlyRule`.** Most rules `return` early for `ctx.order.side.is_risk_reducing` (exits and closing hedges) so a loss halt or exposure cap can never trap the operator in a losing position. That is only safe because `ReduceOnlyRule` is **not** exempted: a closing order may only reduce an existing same-direction position, never exceed it and flip the book short (it also subtracts already-pending closing orders so two exits can't oversell together). Never exempt `ReduceOnlyRule`; never add a risk-reducing exemption to a rule that opens exposure without understanding this pairing.

2. **Short options are sized by strike basis, never premium.** `RiskContext.notional` sizes a `SELL_TO_OPEN` (single or the short legs of a package) at `strike × 100 × qty` — the assignment/margin basis. Premium sizing understates capital at risk by orders of magnitude and lets a naked short slip past buying-power, position, exposure, and leverage caps. Multi-leg packages are deliberately **over-sized** at the sum of their short legs' strike bases (fail-safe — nothing here verifies a long leg covers a short). If a short's strike can't be parsed from its OCC symbol, raise `RiskViolation`; never fall back to premium.

3. **In-flight exposure is reserved so orders in one decision can't stack.** Several orders in a single decision each validate against the same pre-cycle portfolio snapshot. `_validated_notional` (staged at validation) is promoted to `_pending` by `note_order_submitted`, and the rules add `pending_gross` / `pending_options` / `pending_by_symbol` into their exposure math. `_reconcile_pending` releases a reservation only once a portfolio sync taken *after* submission shows the order gone. Risk-reducing orders never reserve. Keep this accounting intact when changing the submit path.

4. **Sleeve caps are attribution-gated — the AI's `strategy` string is not trusted alone.** A strategy's sleeve (a larger per-position cap) applies to an order only if the order's symbol is in `sleeve_attribution[strategy]`, the set the engine built from that cycle's real signals. This stops the model claiming a sleeved strategy's cap for an arbitrary symbol. Sleeves substitute `PositionSizeRule` only — gross exposure, leverage, loss halts, liquidity, and every other rule still apply.

5. **Freshness is enforced, not assumed.** `FreshPortfolioRule` refuses if portfolio state is `>MAX_STATE_AGE_SECONDS` (120s) old or never synced; quotes use `allow_delayed=False`; `PortfolioVaRRule` (when enabled) requires metrics fresher than 1h — *no metrics, no new risk*. Degrade gracefully only for genuinely optional/unknowable inputs (an unclassifiable sector → pass, AI enforces qualitatively); missing **required** data (quote, buy-side volume history, enabled VaR) is a violation.

6. **The daily order counter survives restarts.** `seed_orders_today` rehydrates it from persisted history and `_roll_daily_counter` resets it at the Eastern-midnight boundary, so a mid-session restart can't silently reset `max_orders_per_day`.

## The circuit breaker (`circuit.py`)

Two separate halt mechanisms — don't conflate them:
- **Error-rate breaker** (`CircuitBreaker`): opens when `≥ error_threshold` execution-path errors (broker rejects, data failures, unexpected exceptions) land in a rolling window; auto-closes after a cooldown. `note_execution_error` records into it and publishes `Topics.CIRCUIT_OPENED`. `force_open`/`force_close` is the manual/emergency halt — the audit-chain-corrupt path force-opens it.
- **Loss-limit halts** (`DailyLossRule` / `WeeklyLossRule` / `DrawdownRule`): latched by the rules against portfolio state, independent of the breaker; they clear at the next session boundary, not on a cooldown.

`TradeCooldowns` is the per-symbol re-entry cooldown (exempted for exits).

## Adding or changing a rule

Each rule is a small `RiskRule` subclass with a `name` and a `check(ctx)` that raises `RiskViolation` on breach — nothing else. To add one: implement it, append to `ALL_RULES` in `rules.py`, and cover it in `tests/unit/test_risk.py`.

- **Rules do no I/O.** The engine gathers all live data once and passes it via `RiskContext`; rules are pure and unit-testable with no network. Keep it that way — don't reach for the router or portfolio sync from inside a rule.
- Size everything off `ctx.notional` (which already handles the short-option basis and multi-leg packaging). Money is `Decimal`.
- Decide the risk-reducing exemption deliberately: exempt it for caps/halts/entry-filters, **never** for `ReduceOnlyRule`.
