/* Pure-function unit test for the Alpaca paper⇄live toggle view-logic (spec
 * task 7). No JS test harness in-repo, so this is a node one-off that requires
 * broker_toggle.js (its browser hookup is skipped under node, leaving only the
 * pure helpers) and asserts the load-bearing UI invariants:
 *   - the header badge text/class reflects paper vs LIVE from /api/status;
 *   - the dropdown lists ONLY saved environments (paper_saved/live_saved), and
 *     only while the env-scoped broker is active, so a live account is never
 *     silently offered/selected before its keys exist.
 * Run directly (`node broker_toggle.test.js`) or via the pytest wrapper
 * tests/unit/test_broker_toggle_frontend.py. Exit 0 = pass.
 */
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const { brokerBadge, brokerAccountOptions } = require(
  path.join(__dirname, "..", "..", "src", "poseidon", "api", "static", "broker_toggle.js")
);

/* ---- brokerBadge: topbar #pill-broker ---- */
assert.deepEqual(brokerBadge({ name: "alpaca", paper: true, connected: true }),
  { text: "● alpaca Paper", cls: "pill ok" }, "paper badge is green");
assert.deepEqual(brokerBadge({ name: "alpaca", paper: false, connected: true }),
  { text: "● alpaca LIVE", cls: "pill warn" }, "live badge is amber");
assert.deepEqual(brokerBadge(null),
  { text: "broker —", cls: "pill" }, "no broker → neutral placeholder");

/* ---- brokerAccountOptions: #acct-account-select ---- */
const alpacaEnvKeys = { name: "alpaca", display_name: "Alpaca", credential_paper: "alpaca_paper_keys", credential_live: "alpaca_live_keys" };

// A single-credential broker (no credential_paper) never grows a toggle.
assert.equal(brokerAccountOptions({ name: "tradier", credential: "tradier_creds" }, { name: "tradier", paper: true }).visible,
  false, "non-env broker: no account toggle");

// Alpaca active, only paper saved → paper switch option + Add-live affordance.
{
  const m = brokerAccountOptions({ ...alpacaEnvKeys, paper_saved: true, live_saved: false }, { name: "alpaca", paper: true });
  assert.equal(m.visible, true);
  assert.deepEqual(m.options.map((o) => o.value), ["alpaca:paper", "alpaca:add-live"], "only saved env + add-live");
  assert.equal(m.options[0].selected, true, "active paper account is preselected");
  assert.ok(!m.options.some((o) => o.value === "alpaca:live"), "unsaved live is NOT offered as a switch");
}

// Both saved, live active → both options, live preselected.
{
  const m = brokerAccountOptions({ ...alpacaEnvKeys, paper_saved: true, live_saved: true }, { name: "alpaca", paper: false });
  assert.deepEqual(m.options.map((o) => o.value), ["alpaca:paper", "alpaca:live"], "both saved envs, no add affordance");
  assert.equal(m.options.find((o) => o.value === "alpaca:live").selected, true, "active LIVE account preselected");
  assert.equal(m.options.find((o) => o.value === "alpaca:paper").selected, false);
}

// Env-scoped broker present but NOT the active broker → row hidden (no phantom select).
assert.equal(brokerAccountOptions({ ...alpacaEnvKeys, paper_saved: true, live_saved: true }, { name: "paper", paper: true }).visible,
  false, "toggle hidden when a different broker is active");

// Nothing saved yet → hidden (use the connect form, not the toggle).
assert.equal(brokerAccountOptions({ ...alpacaEnvKeys, paper_saved: false, live_saved: false }, { name: "alpaca", paper: true }).visible,
  false, "no saved envs → hidden");

console.log("broker_toggle.test.js: all assertions passed");
