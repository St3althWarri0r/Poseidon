/* Pure-function unit test for the Settings view logic.
 *
 * No JS test harness in-repo, so this is a node one-off that requires
 * settings_view.js (its browser hookup is a no-op under node) and asserts the
 * presentation invariants that carry real meaning:
 *   - read-only entries never render as an editable control, whatever the UI
 *     wishes — the server's `writable` is obeyed, never re-derived;
 *   - provenance is badged only when someone actually SET the value, so
 *     "why didn't my toggle stick" is answerable at a glance;
 *   - status text never claims a change took effect, because this page cannot
 *     restart the engine;
 *   - guarded settings produce a confirm() naming the real consequence;
 *   - a missing macro leg renders an em-dash marked unavailable, NEVER 0 —
 *     a zero VIX would read as "no volatility", not "no data".
 * Run directly (`node settings_view.test.js`) or via the pytest wrapper
 * tests/unit/test_settings_view_frontend.py. Exit 0 = pass.
 */
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const S = require(
  path.join(__dirname, "..", "..", "src", "poseidon", "api", "static", "settings_view.js")
);

function entry(over) {
  return Object.assign({
    path: "ai.pm_tools.macro_context",
    kind: "bool",
    value: false,
    default: false,
    constraints: {},
    provenance: "default",
    tier: "basic",
    writable: true,
    label: "Tool: macro context",
    help: "Delayed VIX plus the Treasury yield curve.",
    restart: true,
    requires: "",
    registered: true,
  }, over || {});
}

// -- grouping ---------------------------------------------------------------

{
  const groups = S.groupSettings([
    entry({ path: "risk.max_position_pct" }),
    entry({ path: "ai.pm_tools.macro_context" }),
    entry({ path: "screener.enabled" }),
    entry({ path: "zzz_unknown.thing" }),
  ]);
  const names = groups.map((g) => g.name);
  // Curated order first; risk last; an unknown section still appears.
  assert.deepEqual(names, ["ai", "screener", "risk", "zzz_unknown"]);
  assert.equal(groups.find((g) => g.name === "risk").note, S.RISK_NOTE);
  assert.equal(groups.find((g) => g.name === "ai").note, "");
  assert.ok(S.RISK_NOTE.includes("outer limits"));
}

{
  // A brand-new config section must show up on its own, not disappear —
  // that is the whole reason this view is schema-driven.
  const groups = S.groupSettings([entry({ path: "brand_new_feature.enabled" })]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].name, "brand_new_feature");
  assert.equal(groups[0].label, "brand new feature");
}

{
  const sorted = S.sortWithinGroup([
    entry({ path: "a.unregistered", writable: false, registered: false }),
    entry({ path: "a.readonly", writable: false, registered: true }),
    entry({ path: "a.writable", writable: true, registered: true }),
  ]);
  assert.deepEqual(sorted.map((e) => e.path),
    ["a.writable", "a.readonly", "a.unregistered"]);
}

// -- provenance -------------------------------------------------------------

assert.equal(S.provenanceBadge(entry({ provenance: "default" })), null);
assert.equal(S.provenanceBadge(entry({ provenance: "overlay" })).kind, "overlay");
assert.equal(S.provenanceBadge(entry({ provenance: "config file" })).kind, "file");
assert.ok(S.provenanceBadge(entry({ provenance: "overlay" })).text.includes("dashboard"));

// -- control kind -----------------------------------------------------------

assert.equal(S.controlKind(entry({ kind: "bool" })), "toggle");
assert.equal(S.controlKind(entry({ kind: "enum" })), "select");
assert.equal(S.controlKind(entry({ kind: "int" })), "number");
assert.equal(S.controlKind(entry({ kind: "float" })), "number");
assert.equal(S.controlKind(entry({ kind: "str" })), "text");
// Not writable wins over every kind — a read-only field must not render as a
// disabled input that looks briefly clickable.
assert.equal(S.controlKind(entry({ kind: "bool", writable: false })), "readonly");
assert.equal(S.controlKind(entry({ kind: "list", writable: false })), "readonly");

// -- honesty about restart --------------------------------------------------

{
  const note = S.statusNote(entry({ restart: true }));
  assert.ok(note.includes("after a restart"));
  assert.ok(!/took effect|applied now|is live/i.test(note));
  assert.equal(S.statusNote(entry({ restart: false })), "Applies immediately");
  assert.equal(S.statusNote(entry({ writable: false })), "");
  assert.ok(S.statusNote(entry({ requires: "a FUNDAMENTALS provider" }))
    .includes("Needs a FUNDAMENTALS provider"));
}

{
  const msg = S.savedMessage({ applied: ["a"], needs_restart: true });
  assert.ok(msg.includes("Restart Poseidon"));
  assert.ok(!/restarted|now active/i.test(msg));
  assert.ok(S.savedMessage({ applied: ["a", "b"], needs_restart: false })
    .startsWith("Saved 2 settings"));
}

// -- guarded confirm --------------------------------------------------------

{
  assert.equal(S.confirmMessage(entry({ tier: "basic" }), true), "");
  const msg = S.confirmMessage(
    entry({ tier: "guarded", label: "Tool: read web page", help: "Guarded fetch." }), true);
  assert.ok(msg.includes("Tool: read web page"));
  assert.ok(msg.includes("Guarded fetch."));
  assert.ok(msg.includes("next time you start"));
}

// -- coercion + constraints -------------------------------------------------

assert.deepEqual(S.coerce(entry({ kind: "bool" }), true), { ok: true, value: true });
assert.deepEqual(S.coerce(entry({ kind: "int" }), "12"), { ok: true, value: 12 });
assert.equal(S.coerce(entry({ kind: "int" }), "12.5").ok, false);
assert.equal(S.coerce(entry({ kind: "int" }), "abc").ok, false);
assert.deepEqual(S.coerce(entry({ kind: "float" }), "1.5"), { ok: true, value: 1.5 });
assert.equal(S.coerce(entry({ kind: "float" }), "").ok, false);

{
  const e = entry({ kind: "int", constraints: { minimum: 2, maximum: 30 } });
  assert.equal(S.withinConstraints(e, 12).ok, true);
  assert.equal(S.withinConstraints(e, 1).ok, false);
  assert.equal(S.withinConstraints(e, 31).ok, false);
  assert.ok(S.withinConstraints(e, 31).error.includes("at most 30"));
  const ex = entry({ kind: "float", constraints: { exclusiveMinimum: 0 } });
  assert.equal(S.withinConstraints(ex, 0).ok, false);
  assert.equal(S.withinConstraints(ex, 0.1).ok, true);
}

// -- macro strip: unavailable is never zero ---------------------------------

{
  const cells = S.macroCells({
    vix: { level: 14.55, freshness: "delayed" },
    vix_regime: "low",
    term_spread_10y_3m: 0.0081,
    curve_inverted: false,
  });
  assert.equal(cells[0].value, "14.55");
  assert.ok(cells[0].sub.includes("delayed"));
  assert.equal(cells[1].value, "0.81%");
  assert.equal(cells[1].sub, "normal");
}

{
  // Both legs missing. A zero here would be a lie the operator could trade on.
  const cells = S.macroCells({ vix: null, term_spread_10y_3m: null, gaps: ["vix_unavailable"] });
  assert.equal(cells[0].value, "—");
  assert.equal(cells[0].ok, false);
  assert.equal(cells[0].sub, "unavailable");
  assert.equal(cells[1].value, "—");
  assert.equal(cells[1].ok, false);
  assert.equal(S.macroCells({}).length, 2);
  assert.equal(S.macroCells(null)[0].value, "—");
}

{
  const cells = S.macroCells({ term_spread_10y_3m: -0.004, curve_inverted: true });
  assert.equal(cells[1].sub, "inverted");
  assert.equal(cells[1].value, "-0.40%");
}

// -- factor attribution -----------------------------------------------------

{
  // Off (or unavailable) renders nothing at all, not a block of zeros.
  assert.equal(S.factorRows({ factor_attribution: null }), null);
  assert.equal(S.factorRows({}), null);
  assert.equal(S.factorRows(null), null);
}

{
  const rows = S.factorRows({
    factor_attribution: {
      alpha_annual: 0.0421, t_alpha: 2.31, r2: 0.87, n_days: 150,
      loadings: { mkt_rf: 1.02, smb: 0.41, hml: -0.18 },
    },
  });
  const labels = rows.map((r) => r.label);
  assert.ok(labels.includes("Alpha (annual)"));
  assert.ok(labels.includes("Size (SMB)"));
  assert.equal(rows[0].value, "4.21%");
  assert.ok(rows[0].hint.includes("net of market, size and value"));
  assert.equal(rows[1].value, "2.31");
  assert.ok(rows[rows.length - 1].hint.includes("150 days"));
}

{
  // A null t-stat is a gap, shown as such rather than as 0.00.
  const rows = S.factorRows({
    factor_attribution: { alpha_annual: 0.01, t_alpha: null, r2: null, n_days: 70,
                          loadings: { mkt_rf: 1.0 } },
  });
  assert.equal(rows[1].value, "—");
  assert.equal(rows[rows.length - 1].value, "—");
}

// -- pending values (saved but not yet loaded) ------------------------------

{
  // The subtle failure this view exists to prevent: after a restart-required
  // save the engine still holds the old value, so rendering `value` would snap
  // the control back and look exactly like the save was lost.
  const pend = entry({ value: false, pending_value: true, pending: true,
                       provenance: "overlay" });
  assert.equal(S.displayValue(pend), true);
  assert.equal(S.provenanceBadge(pend).kind, "pending");
  assert.ok(S.provenanceBadge(pend).text.includes("pending restart"));
  const note = S.statusNote(pend);
  assert.ok(note.includes("Saved"));
  assert.ok(note.includes("still running"));
}

{
  // Nothing pending: display the live value and badge normally.
  const settled = entry({ value: true, pending_value: true, pending: false,
                          provenance: "config file" });
  assert.equal(S.displayValue(settled), true);
  assert.equal(S.provenanceBadge(settled).kind, "file");
  assert.ok(S.statusNote(settled).includes("after a restart"));
}

console.log("all assertions passed");
