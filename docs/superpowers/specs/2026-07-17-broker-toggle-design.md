# Design spec — Alpaca Paper ⇄ Live account toggle

Status: proposed · Author: Claude · Date: 2026-07-17 · Target: Poseidon (private autonomous trading platform)

## Goal

Let the operator keep BOTH an Alpaca paper account and an Alpaca live (real-money) account saved,
and flip the active brokerage between them from a dropdown in the Account view — no credential
re-entry, no restart — reusing the production-grade `ApplicationKernel.switch_broker` path. A compact
header badge always shows which account is live. Switching TO live is guarded so a mis-click can never
arm autonomous real-money trading.

## Non-goals

- No new broker plugin, no new order path, no change to the risk engine. Orders still flow only through
  `OrderManager._process_order` (invariant #2).
- Not generalising the two-credential model to tradier/tastytrade/ibkr. They keep their single
  credential today; env-scoping them is future work (mechanism below is built to extend, but only Alpaca
  is wired).
- No new persistent store. "Known accounts" is derived from the vault + the existing broker overlay.
- Does not change how the connect FORM enters new credentials — the form is how you first save each
  account; the dropdown only toggles between already-saved ones.

## Two-account model

**Vault credentials become environment-scoped for Alpaca (the only broker whose paper/live are distinct
accounts with distinct API keys + base URLs — `_PAPER`/`_LIVE` in `brokers/plugins/alpaca.py:29-30`):**

- `alpaca_paper_keys` — JSON `{"key_id","secret_key"}` for `https://paper-api.alpaca.markets`
- `alpaca_live_keys`  — JSON `{"key_id","secret_key"}` for `https://api.alpaca.markets`

**"A profile exists" = its credential name is present in `vault.names()`.** No separate registry: the
active selection is already persisted by the existing broker overlay (`poseidon.local.yaml`, one primary
`BrokerConfig` carrying `paper` + `credential`, written by `_write_broker_overlay` app.py:524). Presence
of the two vault credentials is the source of truth for "which accounts can I toggle to."

**Catalog change** (`brokers/registry.py` `_CONNECT_META["alpaca"]`, ~105-113): add per-env credential
names alongside the legacy single one —
```python
"alpaca": {
    "credential": "alpaca_keys",            # legacy; kept for migration + back-compat
    "credential_paper": "alpaca_paper_keys",
    "credential_live":  "alpaca_live_keys",
    "fields": [...unchanged...],
    "paper_choice": "toggle",
    ...
}
```
`broker_catalog()` (registry.py:196-205) passes `credential_paper`/`credential_live` through when present.

**Credential resolution becomes paper-aware** in `_broker_config_for(name, *, paper, options)`
(app.py:357-375) — TODAY it matches the existing config broker by NAME only and would hand a paper
switch the live credential. Change the credential pick (options-by-name inheritance stays):
```python
existing_env = next((b for b in self.config.brokers
                     if b.name == name and b.paper == paper and b.credential), None)
credential = existing_env.credential if existing_env else _env_credential(entry, paper)
```
`_env_credential(entry, paper)` returns `entry["credential_paper"|"credential_live"]` when present, else
`entry["credential"]` (unchanged behavior for every other broker). This single method feeds BOTH
`broker_connection_test` (app.py:386) and `switch_broker` (app.py:428), so Test and Connect both resolve
the right env credential. The overlay dedup (by name, app.py:543) is left alone — only one primary
alpaca entry persists at a time; the vault (which retains both `alpaca_*_keys`) is what makes the other
profile reachable, and `_env_credential` supplies the right name when no matching-env entry exists.

**Migration from single `alpaca_keys`:** on kernel start, if `alpaca_keys ∈ vault.names()` and neither
env name exists, copy it to `alpaca_{paper|live}_keys` per the current alpaca `BrokerConfig.paper`
(default paper) and rewrite that overlay entry's `credential`. **Do NOT delete `alpaca_keys`** — the
Alpaca *data provider* references it as a `ProviderConfig.credential` (see
`tests/unit/test_config_alpaca_provider.py:11`); deleting it would break market data. One-time,
idempotent, audited (`audit.append("system","broker.credential_migrated",...)`).

## Toggle UI (vanilla JS, no framework — matches app.js idioms)

**Header badge** — add one pill to the topbar pill row (`index.html:62-67`, after `#pill-circuit`):
```html
<span class="pill" id="pill-broker" title="Active brokerage account">broker —</span>
```
Render in `refreshStatus()` (app.js ~84-114) from `s.broker` (`/api/status` returns
`{name, paper, connected}`, app.py:1195):
```js
const bk = $("#pill-broker");
bk.textContent = "● " + s.broker.name + (s.broker.paper ? " Paper" : " LIVE");
bk.className = "pill " + (s.broker.paper ? "ok" : "warn");   // green paper / amber live
```

**Dropdown** — in the "Active broker" card (`index.html:209-218`), add above `#acct-current`:
```html
<div class="form-row" id="acct-account-row" hidden>
  <label class="wide">Active account
    <select id="acct-account-select"></select>
    <small id="acct-account-hint"></small></label></div>
```
Populate in `refreshAccount()` (app.js:1290-1316) from the alpaca catalog entry + the new saved flags
returned by `/api/brokers` (below). Build one `<option value="alpaca:paper|alpaca:live">` per SAVED
env; add a disabled "Add live account…" hint that reveals the existing connect form (pre-checking
`#bf-paper` off) when the live credential is not yet saved. `value` encodes `name` + `paper`.

`change` handler (new, near the broker-form handlers ~1482):
```js
$("#acct-account-select").addEventListener("change", async (e) => {
  const [name, env] = e.target.value.split(":");
  const paper = env === "paper";
  if (!paper && !window.confirm(
      "Switch to your LIVE real-money account?\n\n" +
      "Trading mode will drop to APPROVAL — you must deliberately re-enable Autonomous.")) {
    refreshAccount(); return;                    // declined → revert selection, no-op
  }
  try {
    await postJSON("/api/brokers/connect", { name, paper });   // no credentials → reuse vault
    toast(paper ? "Switched to Alpaca Paper" : "Switched to Alpaca LIVE", paper ? "good" : "warn");
  } catch (err) { toast("Switch failed: " + err.message, "bad"); refreshAccount(); }
  refreshStatus(); refreshAccount(); refreshPortfolio();
});
```
Switching to paper is instant (no confirm). The confirm here is UX; the real guard is server-side.

## Backend

**Endpoint the toggle calls: the EXISTING `POST /api/brokers/connect`** (server.py:616-627) with
`{name:"alpaca", paper:bool}` and NO `credentials` — `switch_broker(..., credentials=None)` then reuses
the vault credential via `_build_broker` → `self.vault.get_json(cfg.credential)` (app.py:328-329). No new
endpoint needed; the toggle is just a credential-less connect. (`_broker_request`, server.py:571-602,
already rejects a non-boolean `paper` and defaults it to `True`, so an empty select can't silently pick
live.)

**`/api/brokers` (GET, server.py:543-569)** gains per-env saved flags so the dropdown knows what exists.
For the alpaca entry (and any entry carrying `credential_paper`/`credential_live`):
```python
entry["paper_saved"] = entry.get("credential_paper") in saved_names
entry["live_saved"]  = entry.get("credential_live")  in saved_names
```
(`saved_names = set(kernel.vault.names())` already computed at server.py:548.)

**Server-side live guard — enforced in `ApplicationKernel.switch_broker` (app.py:407-517), the single
choke point.** `switch_broker` is called ONLY from the connect endpoint (server.py:623); startup uses
`_build_broker()` directly (app.py:143), so the guard never fires at boot. Placing it here means the
toggle AND the legacy connect form AND any future caller are all covered — it cannot be bypassed by
hitting the API directly. Insert AFTER the `finally: end_broker_switch()` (app.py:494) and BEFORE the
notification build (app.py:501), so the notify body's mode string is accurate:
```python
if not new_broker.is_paper and self.order_manager.mode is TradingMode.AUTONOMOUS:
    # Real-money account just went active while armed for autonomous trading.
    # Demote to approval so a mis-click can never auto-execute real money; the
    # operator must deliberately re-arm Autonomous. set_mode() audits the change.
    await self.set_mode(TradingMode.APPROVAL)
```
Demotion-only: RESEARCH stays RESEARCH (never raise risk), APPROVAL stays APPROVAL, only
AUTONOMOUS→APPROVAL. Switching to paper never changes mode. `TradingMode` is already imported
(app.py:45); `set_mode` (app.py:1145) writes the `mode.changed` audit record.

## Switch flow (reuses switch_broker unchanged except the guard)

1. Dropdown → `POST /api/brokers/connect {name:"alpaca", paper}` (no creds).
2. `switch_broker` drains in-flight orders (`begin_broker_switch`) and **refuses if any order is open**
   (app.py:421-427, `ConfigError`).
3. Builds + **proves** the new connection via `_build_broker` (auth + account fetch), loading the
   env-scoped saved credential.
4. Persists the overlay (`_write_broker_overlay`); on persist failure drops the proven connection and
   raises — nothing swapped (app.py:441-448).
5. Swaps broker on `order_manager` + `sync`, **resets account state** (`portfolio.account=None`,
   `synced_at=None` ⇒ risk engine refuses to trade until the new account's first sync), disarms exit
   plans, reloads THIS account's scoped baselines (app.py:451-479).
6. **Live guard:** AUTONOMOUS→APPROVAL when the new broker is live.
7. First sync + notify. Header badge + dropdown refresh on the client.

## Failure modes

- **Open orders at the current account** → `switch_broker` raises `ConfigError` with the existing
  message "N order(s) are still open at <broker> — cancel them or let them finish before switching
  brokers" (app.py:423-427) → endpoint returns 422 → toast; active broker unchanged; dropdown reverts.
- **New connection fails** (bad/expired keys, Alpaca down) → `_build_broker` raises before any swap;
  `self.broker` untouched (app.py:429). 422 → toast.
- **Live confirm declined** (client) → handler returns without POSTing; `refreshAccount()` restores the
  select to the true active account. No-op.
- **Overlay persist fails** → proven connection dropped, `ConfigError` (app.py:441-448); no swap.
- **Live credential not yet saved** → option is not offered (only `live_saved` envs appear); the "Add
  live account…" affordance opens the connect form instead.

## Security / safety checklist

- [x] Real-money guard is **server-side** in `switch_broker` (app.py, after :494) — unbypassable via any
      HTTP path; client confirm is UX only.
- [x] Demotion-only mode clamp: live switch can never *raise* autonomy; AUTONOMOUS→APPROVAL, audited via
      `set_mode`.
- [x] `switch_broker` race-guards intact — `begin/end_broker_switch` + open-order refusal + forced
      account-state reset are inherited unchanged (no order decided against one account reaches another).
- [x] Risk engine never bypassed: `portfolio.account=None` after swap forces a fresh sync before any
      order; the one order path is untouched.
- [x] Secrets only in the vault: config/overlay hold credential NAMES (`alpaca_paper_keys` /
      `alpaca_live_keys`), never values (invariant #6). Migration copies within the vault.
- [x] Every switch already audits `broker.switched` (app.py:480-482); the mode clamp adds `mode.changed`.
- [x] Alpaca *data provider* credential (`alpaca_keys`) preserved by migration.

## Ordered TDD task list (backend first, each step red→green)

1. **Catalog env credentials** — add `credential_paper`/`credential_live` to alpaca meta;
   `broker_catalog()` surfaces them. Test: entry has both names; other brokers unchanged.
2. **`/api/brokers` saved flags** — endpoint returns `paper_saved`/`live_saved` from a fake vault. Test:
   seed only `alpaca_paper_keys` ⇒ `paper_saved` true, `live_saved` false.
3. **Env-scoped credential resolution** — `_broker_config_for("alpaca", paper=True)` ⇒
   `alpaca_paper_keys`; `paper=False` ⇒ `alpaca_live_keys`, even when config holds the opposite-env
   entry. Test: assert `cfg.credential` per env.
4. **Connect-with-saved-creds** — `switch_broker("alpaca", paper=True, credentials=None)` loads
   `alpaca_paper_keys` from the vault (fake broker + seeded vault, no re-entry). Test: broker built with
   those creds; `is_paper` correct.
5. **Live guard (server-side)** — mode AUTONOMOUS + switch to live ⇒ mode APPROVAL + `mode.changed`
   audit; switch to paper leaves mode; RESEARCH/APPROVAL unchanged; assert it also fires through
   `POST /api/brokers/connect`. Test in `tests/unit/` with the fake broker.
6. **Migration** — legacy `alpaca_keys` present, env names absent ⇒ copied to the current-env name;
   `alpaca_keys` retained; idempotent on second run. Test.
7. **Frontend (lighter)** — dropdown lists only saved envs; selecting live triggers `window.confirm`
   then a credential-less POST; header badge class/text reflect `/api/status`. Cover with a small JS unit
   or an optional Playwright check (`tests/` has playwright available); manual smoke acceptable.

## Existing tests that might change

- `tests/unit/test_account_chat.py:37-67` (catalog shape) — assertions use `>=`/field-presence, so
  additive fields pass as-is; ADD assertions for the two new alpaca credential names.
- `tests/unit/test_config_alpaca_provider.py:11` — must keep passing: migration must NOT remove
  `alpaca_keys` (data-provider credential). Guard test that this still resolves.
- `tests/unit/test_sync_baselines.py` — already exercises `account_scope = name:paper|live`; the toggle
  is exactly the "same-scope-name, different account" path — re-verify paper↔live re-anchors baselines
  (no change expected, but this feature stresses it).
- `POST /api/brokers/connect` docstring (server.py:618-620) "The trading mode is untouched" and the
  Account-view copy (index.html:216-217, 251-253, form-submit confirm app.js:1487-1490) — reword: a LIVE
  activation now demotes Autonomous→Approval. No test currently pins "mode preserved across a live
  connect," so this is additive behavior, not a regression.
- Add a new `tests/unit/test_broker_toggle.py` for tasks 3-6 (none exists today; broker-switch coverage
  lives across `test_sync_baselines.py` + `test_account_chat.py`).
