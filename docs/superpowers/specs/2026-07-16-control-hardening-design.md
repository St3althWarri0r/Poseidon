# Operator Control Hardening — Design Spec

**Date:** 2026-07-16
**Status:** Approved (design), pending implementation plan
**Target:** Poseidon v2.13.0 candidate
**Origin:** Rank **3** of cross-pollination round 2 (program.md §3). Deterministic control-plane
only — **no LLM touches any code path in this spec** (invariant 1). All facts verified against
`src/poseidon/{app.py, execution/manager.py, risk/{engine,rules,circuit}.py, core/config.py}`.

## 1. Goal

Close the last three deterministic control-plane holes: (a) `kernel.halt()` leaves resting broker
orders live (they can fill mid-halt) — halt now cancels them, then optionally (default OFF)
flattens positions through a provably narrow breaker carve-out; (b) the AI can order any symbol —
a deterministic `UniverseRule` denies opens outside the configured universe while risk-reducing
exits always pass; (c) an autonomy grant never expires — `mode.autonomous_expires_at` auto-reverts
AUTONOMOUS→APPROVAL, idempotently and restart-safely.

## 2. Non-goals

- **`min_market_cap` universe bound — descoped.** No provider/router market-cap capability exists
  (verified: zero hits in `src/poseidon/data/`). A floor that fail-opens on missing data is a false
  guarantee; fail-closed halts all entries on a data gap. Land after rank 4 ships fundamentals (§10).
- No rule exemptions keyed off the halt token. The carve-out bypasses **only the two circuit-
  breaker checks**; every risk rule (incl. `ReduceOnlyRule`, `OrdersPerDayRule`, `MarketOpenRule`,
  `SlippageProtectionRule`) still runs on halt-exits. Denials are recorded, never forced.
- No flushing of `PENDING_APPROVAL` orders on halt (not at the broker; a post-halt approval is
  already killed by the post-approval re-validate, manager.py:249–267). No retry loops anywhere:
  cancels attempted exactly once; flatten reuses the bounded `_SUBMIT_RETRIES` machinery unchanged.
- Mode persistence across restarts stays config-sourced (unchanged); only the *expiry* is durable.

## 3. Feature A — halt(): cancel-all, then opt-in flatten

### 3.1 Current code (app.py:1155)
`halt()` = breaker `force_open` → HALT sentinel file → kv `circuit.manual_halt` → audit. Nothing
touches orders. Breaker checks: `validate_order` (engine.py:149) + pre-submit re-check inside
`_submit_lock` (manager.py:346).

### 3.2 New order of operations in `kernel.halt(reason)` (signature unchanged)
Serialized with `resume()` via a new `kernel._halt_lock: asyncio.Lock`.

1. **Latch first, always** (existing lines, unchanged order): `force_open` → sentinel → kv →
   audit `trading.halted`. The latch is synchronous — trading is dead before any I/O below.
2. **Cancel-all** — `await self.order_manager.cancel_all_open(reason=reason)` (always on halt):
   - Enumerate from DB: `status IN (SUBMITTED, ACCEPTED, PARTIALLY_FILLED)`. Rows with
     `order.broker != self._broker.name` → skipped + recorded (never cancel cross-broker,
     mirroring `cancel()`'s guard). If `_switching` → abort cleanup, record.
   - Each order: `await broker.cancel_order(order)` **once**; success → persist + audit (`human`,
     `halt.order_canceled`); any exception → audit (`system`, `halt.cancel_failed`,
     {order_id, symbol, error}), append to the failure list, **continue the loop**. No retries.
   - Returns frozen `HaltCleanupSummary(canceled, failed, skipped)` (manager.py dataclass).
   - A `PARTIALLY_FILLED` cancel kills the remainder; the filled portion is already in the live
     position and is handled by flatten sizing (3.3).
3. **Flatten (opt-in, `risk.flatten_on_halt`, default `False`)** — skipped entirely when off
   (no `positions()` call). When on and mode is not RESEARCH:
   - **Verify quiet book:** one live `broker.open_orders()` fetch. Any symbol still carrying an
     open order (cancel failed / pending-cancel / cross-broker skip) is **excluded from flatten**
     and recorded (`halt.flatten_refused`, `open_order_survives`) — the guarantee that a resting
     order cannot fill against a closing trade. `open_orders()` or `positions()` failure → skip
     flatten entirely, audit + critical notify (cannot verify → do not trade).
   - `token = self.risk.open_halt_flatten_window()`; `try:` exits `finally: close_…_window()`.
   - Per non-zero live position, one reduce-only **MARKET DAY** order, `strategy="halt_flatten"`,
     sized to live quantity: equity/ETF/crypto long → `SELL`; short (qty<0, external origin) →
     `BUY_TO_CLOSE abs(qty)`; option long → `SELL_TO_CLOSE`, short → `BUY_TO_CLOSE`. Never multi-leg.
   - Submit via `order_manager.flatten_all(token, reason=...)`: per order, sequentially, `_persist`
     → `risk.validate_order(order, halt_token=token)` → `_submit(order, halt_token=token)`. The
     **full rule chain runs** (ReduceOnlyRule, live `_guard_reduce_only` backstop, duplicate guard,
     capability gate, preflight — all unchanged). Any denial → `REJECTED_RISK` persisted + audited
     (`halt.flatten_refused`); **not retried**. The approval queue is **never** consulted — the
     operator's halt IS the human consent (actor `human` in `halt.flatten_submitted`).
4. **One summary notification** (`critical`): canceled n / cancel-failed m (listed) / flattened k
   / refused j (rule names). Cleanup exceptions (phases 2–3) are caught at the `halt()` level:
   audit `halt.cleanup_failed` + critical notify — **the latch always stands**.

### 3.3 Known, documented behavior (not bugs)
- Cancel-all removes resting **guardian protective stops** too; with flatten OFF the book sits
  unprotected until resume (guardian re-fires are breaker-blocked — existing semantics). The
  summary notification says so explicitly.
- Flatten is best-effort through the normal rules: after-hours halt → `MarketOpenRule` denies
  equity exits (crypto passes); a dislocated/one-sided book → `SlippageProtectionRule` denies the
  market exit; a large book late in the day → `OrdersPerDayRule` may deny trailing exits (each
  flatten exit consumes the daily budget). All denials are loud.

### 3.4 The carve-out mechanism (the sharp edge)
Capability token, identity-checked, engine-owned:

```python
# risk/engine.py
def open_halt_flatten_window(self, *, ttl_seconds: float = 300.0) -> object:
    self._halt_flatten_token: object | None = object()   # unforgeable: identity IS the capability
    self._halt_flatten_deadline = time.monotonic() + ttl_seconds
    return self._halt_flatten_token

def close_halt_flatten_window(self) -> None:             # kernel.halt() calls in finally
    self._halt_flatten_token = None

def halt_exit_permitted(self, order: Order, token: object | None) -> bool:
    return (token is not None and token is self._halt_flatten_token
            and time.monotonic() < self._halt_flatten_deadline
            and order.side.is_risk_reducing and not order.legs)

# Both breaker checks — validate_order(…, *, halt_token: object | None = None) at engine.py:149
# and _submit's pre-submit re-check at manager.py:346 — become this one shape:
if self.circuit.is_open and not (
        halt_token is not None and self.halt_exit_permitted(order, halt_token)):
    raise CircuitBreakerOpen(...)   # in _submit: the existing REJECTED_RISK block, unchanged
```

**`halt_token is not None` short-circuits first**: every existing caller (all pass nothing) never
consults the window, and mocked-RiskEngine test stacks keep their semantics. **Provably narrow:**
a bare `object()` cannot be forged (identity), serialized, persisted, or replayed, and never rides
on the `Order` model or any pydantic schema — no AI/chat/API/decision payload can carry one.
`execute_decision`, `submit_manual`, and guardian `_dispatch_exit` (routes through
`execute_decision`, guardian.py:274) have **no token parameter**; the only holder is
`kernel.halt()` → `flatten_all`. Even a stolen live token admits only reduce-only, leg-free
orders, only while the window is open and under the deadline.

## 4. Feature B — UniverseRule

New rule in `risk/rules.py`, registered in `ALL_RULES` after `MarketOpenRule` (cheap string check,
early denial; list position is not load-bearing — every rule runs until the first violation).

```python
class UniverseRule(RiskRule):
    name = "universe"
    def check(self, ctx):
        if ctx.order.side.is_risk_reducing and not any(
                leg.side in (OrderSide.BUY_TO_OPEN, OrderSide.SELL_TO_OPEN)
                for leg in ctx.order.legs):
            return  # risk-reducing exits ALWAYS pass — never trap a position outside the universe
        symbol = _underlying(ctx.order)   # OCC root for options/legs, else order.symbol, uppercased
        if symbol in set(ctx.config.universe_exclude_symbols):
            raise RiskViolation(self.name, f"{symbol} is on universe_exclude_symbols")
        allow = ctx.config.universe_allow_symbols
        if allow and symbol not in set(allow):
            raise RiskViolation(self.name, f"{symbol} is not on universe_allow_symbols")
```

- `_underlying`: strip a trailing OCC tail (`\d{6}[CP]\d{8}`) for OPTION orders; multi-leg parents
  already carry the underlying (rules.py:538). Denial is by **underlying** — an excluded equity
  cannot be re-entered via its options.
- Exclude wins over allow (checked first). Both empty = rule passes everything (ships as a no-op;
  conservative default = no behavior change).
- Applies to **every** open — AI decisions and the operator's own `submit_manual` tickets (manual
  orders pass every rule, manager.py:276; the config file is the override). Purely sync +
  config-driven: no data fetch, no LLM, deterministic.
- Config: flat `RiskConfig` fields, uppercase-normalizing validator (à la `WatchlistConfig._upper`):
  `universe_exclude_symbols: list[str] = []`, `universe_allow_symbols: list[str] = []`.

## 5. Feature C — autonomous-mode consent expiry

### 5.1 State & config
- kv key **`mode.autonomous_expires_at`** (ISO-8601 UTC; cleared by writing `""`, the
  `circuit.manual_halt` convention). Durable in the existing `kv` table (db.py:50).
- Config: `risk.autonomous_ttl_hours: float = 0` (`ge=0`; **0 = no expiry, current behavior**).

### 5.2 Where the grant is set
- **Operator action** (primary): `POST /api/mode` gains optional `expires_in_hours` (float) /
  `expires_at` (ISO), accepted only with `mode=autonomous` → `kernel.set_mode(mode, expires_at=...)`.
  `set_mode(AUTONOMOUS, ...)` **always rewrites** the key: explicit value → that; else ttl>0 →
  now+ttl; else clear. Audits `mode.autonomy_granted {expires_at}` alongside `mode.changed`.
  `set_mode(non-AUTONOMOUS)` clears the key (grant consumed).
- **Startup** (config `mode: autonomous`): never extends — an existing key is honored as-is
  (future → keep; past → immediate revert, 5.3); absent + ttl>0 → stamp now+ttl; absent + ttl=0 →
  unbounded (status quo). A crash-restart loop therefore **cannot re-arm expired autonomy**: the
  stale key latches until an explicit operator re-grant.

### 5.3 The checker — `kernel._check_autonomy_expiry() -> bool`
```
if order_manager.mode is not AUTONOMOUS: return False       # idempotent no-op, no kv read
expires = await db.kv_get("mode.autonomous_expires_at");  if not expires: return False
parse; unparseable -> EXPIRED (fail-safe: a corrupt bound must not grant unbounded autonomy)
if now_utc < expires_at: return False
order_manager.set_mode(APPROVAL)   # direct, NOT kernel.set_mode — the kv latch must survive
audit("system", "mode.autonomy_expired", {expires_at})
bus.publish NOTIFY critical: "Autonomous mode expired — reverted to approval mode";  return True
```
Idempotence: a second call sees APPROVAL → returns at line 1; no duplicate notification is
possible in-process. Across restarts it re-fires **only** if something re-armed AUTONOMOUS
(config at boot) — that is a genuine new event and must notify.

### 5.4 Check sites (all three)
1. **Startup**: in `start()`, after `_restore_manual_halt()` and before `scheduler.start()`
   (notifier is constructed by then) — apply 5.2-startup stamping, then run the checker.
2. **Scheduler job** `autonomy_expiry`, `every_seconds=60`, `only_market_hours=False`, registered
   unconditionally in `_register_jobs` + `_effective_schedules` — fires overnight, no AI needed.
3. **Cycle start**: first statement inside `run_review_cycle`'s `_cycle_lock`, so the cycle prompt
   (`agent.run_cycle(mode=...)`) sees the reverted mode.

### 5.5 In-flight decisions at the boundary
`OrderManager._process_order` re-reads `self._mode` **per order** at execution time
(manager.py:189,227): a revert landing mid-cycle routes every not-yet-processed trade into the
approval queue; already-submitted orders, pollers, and guardian plans are untouched. Worst-case
autonomous overshoot ≈ one 60 s job tick + the order already inside the gate. All checkers share
the kernel's single event loop (engine.py:180 LOAD-BEARING note) — no extra locking.

## 6. Config, state & audit surface (all new behavior config-gated, conservative defaults)

| Kind | Key | Default | Meaning |
|---|---|---|---|
| config | `risk.flatten_on_halt` | `False` | opt-in reduce-only flatten after halt cancel-all |
| config | `risk.universe_exclude_symbols` | `[]` | denylist (opens denied; exits pass) |
| config | `risk.universe_allow_symbols` | `[]` | allowlist; empty = allow all |
| config | `risk.autonomous_ttl_hours` | `0` | default grant TTL; 0 = never expires |
| kv | `mode.autonomous_expires_at` | absent | durable consent bound; latches past expiry |
| audit | `halt.order_canceled` / `halt.cancel_failed` / `halt.flatten_submitted` / `halt.flatten_refused` / `halt.cleanup_failed` | — | hash-chained halt-cleanup facts |
| audit | `mode.autonomy_granted` / `mode.autonomy_expired` | — | hash-chained consent facts |

Cancel-all on halt is the one non-gated change: it **is** the fix (program.md rank 3(a)), and its
failure mode is loud, not silent.

## 7. Reviewer safety checklist

1. **Carve-out reachability**: the only `open_halt_flatten_window()` caller is `kernel.halt()`;
   the only token-accepting entry point is `flatten_all`. `execute_decision` / `submit_manual` /
   guardian `_dispatch_exit` / `/api/*` cannot pass a token (no parameter exists); a tripped
   breaker still rejects ALL of those paths — including risk-reducing guardian exits (unchanged).
2. **Token hygiene**: identity-checked `object()`; never on `Order`/schemas/DB; window closed in
   `finally`; deadline bounds a crashed flatten; permit requires reduce-only + leg-free.
3. **ReduceOnlyRule untouched**: rule exemptions unchanged; flatten exits run the full chain AND
   the live `_guard_reduce_only` submit backstop (F022) — verify no exemption keyed on
   `strategy == "halt_flatten"` or the token exists anywhere.
4. **Ordering**: no flatten submit precedes completion of the cancel pass + quiet-book
   verification; symbols with surviving open orders are excluded, not raced.
5. **Cancel failures**: exactly one `cancel_order` call per order; failure recorded + notified,
   loop continues, halt latch unaffected.
6. **Exits pass the denylist**: `UniverseRule` returns early for risk-reducing sides — an excluded
   symbol's position can always be closed; option opens denied by underlying.
7. **Expiry idempotence**: checker no-ops when not AUTONOMOUS; revert does NOT clear the kv latch;
   startup never extends a grant; unparseable timestamp = expired.

## 8. TDD task list (ordered; tests written first in each task)

1. **UniverseRule** — `tests/unit/test_risk.py::TestUniverseRule`: `test_open_denied_on_exclude`,
   `test_open_denied_off_allowlist`, `test_exit_passes_even_when_excluded`,
   `test_option_open_denied_by_underlying`, `test_case_insensitive`, `test_empty_config_is_noop`.
   Implement rule + `RiskConfig` fields + `ALL_RULES` entry. Done: green, no other rule test touched.
2. **Flatten window on RiskEngine** — `tests/unit/test_risk.py::TestHaltFlattenWindow`:
   `test_forged_token_rejected` (fresh `object()` → `CircuitBreakerOpen`),
   `test_closed_window_rejects_real_token`, `test_deadline_expires_token`,
   `test_valid_token_never_admits_opening_order`, `test_valid_token_admits_reduce_only_exit`.
   Implement open/close/permit + `validate_order` kwarg. Done: default-arg callers unaffected.
3. **Manager token thread + adversarial breaker tests** — `tests/unit/test_p1_manager.py`:
   `test_tripped_breaker_blocks_execute_decision_buy_and_sell`,
   `test_tripped_breaker_blocks_submit_manual`, `test_tripped_breaker_blocks_guardian_dispatch`
   (via `execute_decision`), `test_submit_recheck_honors_active_token`. Implement `_submit`
   kwarg + reshaped pre-submit predicate. Done: existing F017 test still green un-edited.
4. **`cancel_all_open`** — `test_halt_cleanup.py`: `test_cancels_each_open_order_once`,
   `test_cancel_failure_recorded_not_retried` (raise once → exactly 1 call, loop continues),
   `test_cross_broker_rows_skipped_and_recorded`, `test_returns_summary`. Implement.
5. **`flatten_all`** — `test_halt_cleanup.py`: `test_builds_reduce_only_market_exits`
   (long SELL / short BUY_TO_CLOSE / option SELL_TO_CLOSE), `test_skips_symbols_with_surviving_open_orders`,
   `test_research_mode_refuses`, `test_rule_denial_recorded_not_retried`,
   `test_reduce_only_rule_still_consulted` (oversized exit → `RiskViolation("reduce_only")`).
   Implement (approval queue bypassed; audits as `human`).
6. **`kernel.halt()` orchestration** — `test_halt_cleanup.py`:
   `test_latch_precedes_any_broker_call`, `test_cancel_completes_before_first_flatten_submit`
   (call-order instrumentation on a fake broker), `test_flatten_off_by_default_no_positions_call`,
   `test_partial_fill_cancel_remainder_then_flatten_filled_position`,
   `test_cleanup_failure_keeps_latch`, `test_audit_facts_chain`. Implement phases + `_halt_lock`
   + summary notification.
7. **Expiry checker + grant plumbing** — `test_autonomy_expiry.py`:
   `test_expiry_reverts_and_notifies_critical`, `test_already_approval_is_noop_no_duplicate_notify`,
   `test_unparseable_expiry_treated_expired`, `test_expiry_idempotent_across_restart`
   (kv persists; boot with config-AUTONOMOUS + past key → revert again, notify once),
   `test_startup_never_extends_grant`, `test_ttl_stamped_on_grant`,
   `test_explicit_expiry_overrides_ttl`, `test_set_mode_non_autonomous_clears_grant`.
   Implement `_check_autonomy_expiry`, `set_mode(expires_at=...)`, kv writes.
8. **Wiring: scheduler job + cycle-start + API** — `test_autonomy_expiry.py`:
   `test_job_registered_and_fires_without_ai`, `test_cycle_start_checks_expiry`,
   `test_api_mode_accepts_expires_in_hours`; `test_halt_cleanup.py::test_api_halt_runs_cleanup`.
   Implement `_register_jobs`/`_effective_schedules` entries, `run_review_cycle` hook, `/api/mode`
   body fields. Done: full unit suite green.
9. **Integration + docs** — extend `tests/integration/test_order_flow.py`: halt with a resting
   partially-filled order + `flatten_on_halt=true` end-to-end (remainder canceled → position
   flattened → breaker still blocks a follow-up BUY). Update `docs/risk-controls.md` + user guide
   (halt now cancels protective stops too).

## 9. Existing tests that might break

- `tests/unit/test_p1_manager.py` — F017 pre-submit re-check + mocked-risk `stack` fixtures: the
  reshaped predicate must short-circuit on `halt_token is None` (§3.4) so Mock truthiness never
  enters; verify F017 passes unmodified.
- `tests/unit/test_hardening_vibe.py` — halt-file/`CircuitBreaker` tests: class untouched, should
  hold; any kernel-level halt test (and `/api/halt` API tests) now needs
  `cancel_all_open`/`open_orders` stubs on its mocks — halt performs DB + broker I/O.
- `tests/unit/test_risk.py` / `test_reservations.py` / `tests/integration/test_order_flow.py` —
  additive kwarg + appended no-op-default rule: expected green, but any test enumerating
  `ALL_RULES` by exact contents must add `UniverseRule`.

## 10. Honest deltas vs program.md rank 3

- **min-market-cap descoped** (no market-cap data capability exists; §2) — the universe bound
  ships as exclude+allow lists only.
- Flatten is **best-effort by design**: session, slippage, and daily-order-budget rules can deny
  individual halt-exits (§3.3); expect loud partial flattens in dislocated markets. The
  alternative — rule exemptions keyed off the token — widens the round's sharpest carve-out and
  is rejected.
- Cancel-all changes existing halt semantics for **guardian protective stops** (resting broker
  orders, so canceled too); called out in the summary notification and docs.
