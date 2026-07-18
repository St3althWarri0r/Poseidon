/* Pure view-logic for the Alpaca paper⇄live account toggle (spec task 7).
 *
 * These two functions are the only *pure* parts of the toggle UI — the header
 * badge string/class and the "which saved accounts can I flip between" option
 * model. They live here (not inline in app.js) so a node one-off unit test can
 * exercise them without a DOM: app.js is a plain top-level script and cannot be
 * required under node, whereas this IIFE guards its browser hookup. All DOM
 * wiring, the live confirm(), and the credential-less POST stay in app.js.
 *
 * Loaded before app.js, which reads window.PoseidonBrokerToggle. Under node the
 * module.exports hook feeds tests/frontend/broker_toggle.test.js.
 */
"use strict";

(function () {
  // Header pill (#pill-broker) content for /api/status `broker` {name,paper}.
  // Green "ok" for paper, amber "warn" for a live real-money account.
  function brokerBadge(broker) {
    if (!broker || !broker.name) return { text: "broker —", cls: "pill" };
    const live = broker.paper === false;
    return {
      text: "● " + broker.name + (live ? " LIVE" : " Paper"),
      cls: "pill " + (live ? "warn" : "ok"),
    };
  }

  // Option model for the #acct-account-select dropdown. Only env-scoped brokers
  // (Alpaca today, identified by a credential_paper key) get a toggle, and only
  // while that broker is the ACTIVE one — so exactly one option is ever the
  // current account and there is no phantom "selected" env. Each saved
  // environment (paper_saved / live_saved from /api/brokers) becomes a real
  // switch option; a not-yet-saved environment becomes an "Add …" affordance
  // that reveals the connect form instead of switching.
  //   entry:   catalog entry from /api/brokers.brokers[]
  //   current: /api/brokers.current {name, paper}
  // → { visible, options:[{value,label,selected}], hint }
  function brokerAccountOptions(entry, current) {
    const hidden = { visible: false, options: [], hint: "" };
    if (!entry || !("credential_paper" in entry)) return hidden;
    const cur = current || {};
    const name = entry.name;
    if (cur.name !== name) return hidden; // toggle only among the active broker's envs
    const paperSaved = !!entry.paper_saved;
    const liveSaved = !!entry.live_saved;
    if (!paperSaved && !liveSaved) return hidden; // nothing saved to toggle between
    const label = String(entry.display_name || name);
    // value encodes name + env; the change handler splits on ":".
    const options = [];
    if (paperSaved) options.push({ value: name + ":paper", label: label + " Paper", selected: cur.paper === true });
    if (liveSaved) options.push({ value: name + ":live", label: label + " LIVE", selected: cur.paper === false });
    if (!liveSaved) options.push({ value: name + ":add-live", label: "Add live account…", selected: false });
    if (!paperSaved) options.push({ value: name + ":add-paper", label: "Add paper account…", selected: false });
    const hint = paperSaved && liveSaved
      ? "Flip between your saved accounts — no key re-entry. Activating LIVE drops Autonomous to Approval."
      : "Add the other environment (opens the connect form) to flip without re-entering keys.";
    return { visible: true, options, hint };
  }

  const api = { brokerBadge, brokerAccountOptions };
  if (typeof window !== "undefined") window.PoseidonBrokerToggle = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
