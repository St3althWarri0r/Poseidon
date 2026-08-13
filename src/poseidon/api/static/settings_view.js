/* Pure view-logic for the Settings view.
 *
 * Same shape as model_selector.js: the load-bearing pure parts live here so a
 * node one-off unit test can exercise them without a DOM (app.js is a plain
 * top-level script and cannot be required under node). All DOM wiring, the
 * guarded-tier confirm(), and the POST stay in app.js.
 *
 * Loaded before app.js, which reads window.PoseidonSettings. Under node the
 * module.exports hook feeds tests/frontend/settings_view.test.js.
 *
 * The server is the authority on everything here. Tier enforcement, validation
 * and provenance are all computed in settings_meta.py / apply_settings; this
 * file only decides how to PRESENT them. A control rendered enabled that the
 * server refuses is a cosmetic bug, not a security hole — but the reverse
 * (rendering a read-only field as editable) would mislead, so `writable` from
 * the payload is always obeyed and never re-derived.
 */
"use strict";

(function () {
  // Human ordering for the top-level config sections. Anything not listed
  // sorts alphabetically after these — a new section appears on its own
  // rather than vanishing, which is the whole point of the exercise.
  const GROUP_ORDER = [
    "mode", "ai", "screener", "crypto_screener", "backtest",
    "data", "guardian", "strategy_health", "reports", "updates",
    "notifications", "schedules", "strategies", "brokers", "risk",
  ];

  const GROUP_LABELS = {
    mode: "Trading mode",
    ai: "AI & research tools",
    screener: "Equity screener",
    crypto_screener: "Crypto screener",
    backtest: "Backtest evaluation",
    data: "Market data",
    guardian: "Position guardian",
    strategy_health: "Strategy health",
    reports: "Reports",
    updates: "Updates",
    risk: "Risk limits",
  };

  // Why the risk block is visible but untouchable. Shown once, on that group.
  const RISK_NOTE =
    "Read-only by design. These are the outer limits on an armed autonomous " +
    "trader — a one-click widening control in a web page is the most dangerous " +
    "affordance this app could have. Edit them in poseidon.yaml deliberately.";

  function groupOf(path) {
    const head = String(path).split(".")[0];
    return head;
  }

  function groupLabel(name) {
    if (GROUP_LABELS[name]) return GROUP_LABELS[name];
    return String(name).replace(/_/g, " ");
  }

  // Group the flat settings list into ordered sections for rendering.
  //   entries: GET /api/settings -> .settings
  // → [{ name, label, note, entries: [...] }]
  function groupSettings(entries) {
    const byGroup = new Map();
    (entries || []).forEach((e) => {
      const g = groupOf(e.path);
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g).push(e);
    });
    const names = Array.from(byGroup.keys());
    names.sort((a, b) => {
      const ia = GROUP_ORDER.indexOf(a);
      const ib = GROUP_ORDER.indexOf(b);
      if (ia === -1 && ib === -1) return a.localeCompare(b);
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    });
    return names.map((name) => ({
      name,
      label: groupLabel(name),
      note: name === "risk" ? RISK_NOTE : "",
      entries: byGroup.get(name),
    }));
  }

  // Registered, writable entries first (what someone actually came here to
  // change), then registered read-only, then the unlabelled advanced tail.
  function sortWithinGroup(entries) {
    const rank = (e) => (e.writable ? 0 : e.registered ? 1 : 2);
    return (entries || []).slice().sort((a, b) => {
      const d = rank(a) - rank(b);
      return d !== 0 ? d : String(a.path).localeCompare(String(b.path));
    });
  }

  // Badge describing where the effective value came from. "default" is the
  // quiet case and gets no badge — only a value someone SET is worth marking.
  function provenanceBadge(entry) {
    if (!entry) return null;
    if (entry.provenance === "overlay") {
      return { text: "set in dashboard", kind: "overlay" };
    }
    if (entry.provenance === "config file") {
      return { text: "set in poseidon.yaml", kind: "file" };
    }
    return null;
  }

  // What kind of control to render. Read-only entries render as a value, never
  // as a disabled input that looks momentarily clickable.
  function controlKind(entry) {
    if (!entry || !entry.writable) return "readonly";
    if (entry.kind === "bool") return "toggle";
    if (entry.kind === "enum") return "select";
    if (entry.kind === "int" || entry.kind === "float") return "number";
    if (entry.kind === "str") return "text";
    return "readonly";
  }

  // The one-line status under a control: restart honesty + any dependency.
  // Never claims a change took effect.
  function statusNote(entry) {
    if (!entry || !entry.writable) return "";
    const bits = [];
    bits.push(entry.restart ? "Takes effect after a restart" : "Applies immediately");
    if (entry.requires) bits.push(`Needs ${entry.requires}`);
    return bits.join(" · ");
  }

  // Guarded settings get a confirm() with the real consequence spelled out.
  function confirmMessage(entry, nextValue) {
    if (!entry || entry.tier !== "guarded") return "";
    return (
      `Change "${entry.label}" to ${JSON.stringify(nextValue)}?\n\n` +
      `${entry.help}\n\n` +
      (entry.restart
        ? "This takes effect the next time you start Poseidon."
        : "This applies immediately.")
    );
  }

  // Coerce a raw DOM input value into the type the backend expects. Returns
  // { ok, value } — a non-numeric string for a number field is rejected HERE
  // so the user sees it inline rather than as a 422 round-trip.
  function coerce(entry, raw) {
    const kind = entry && entry.kind;
    if (kind === "bool") return { ok: true, value: !!raw };
    if (kind === "int" || kind === "float") {
      // Number("") and Number("  ") are both 0. Cleared a numeric field and
      // saved? That must be an error, not a silent zero — several of these
      // knobs treat 0 as "disabled", so the blank would quietly turn a feature
      // off rather than leaving it alone.
      const text = String(raw == null ? "" : raw).trim();
      if (text === "") return { ok: false, value: null, error: "cannot be empty" };
      const n = Number(text);
      if (!Number.isFinite(n)) return { ok: false, value: null, error: "must be a number" };
      if (kind === "int" && !Number.isInteger(n)) {
        return { ok: false, value: null, error: "must be a whole number" };
      }
      return { ok: true, value: n };
    }
    return { ok: true, value: String(raw) };
  }

  // Client-side bounds check mirroring the schema constraints, so an
  // out-of-range value is caught before the POST. The server re-checks; this
  // is purely to make the failure legible.
  function withinConstraints(entry, value) {
    const c = (entry && entry.constraints) || {};
    if (typeof value === "number") {
      if (c.minimum != null && value < c.minimum) {
        return { ok: false, error: `must be at least ${c.minimum}` };
      }
      if (c.maximum != null && value > c.maximum) {
        return { ok: false, error: `must be at most ${c.maximum}` };
      }
      if (c.exclusiveMinimum != null && value <= c.exclusiveMinimum) {
        return { ok: false, error: `must be greater than ${c.exclusiveMinimum}` };
      }
      if (c.exclusiveMaximum != null && value >= c.exclusiveMaximum) {
        return { ok: false, error: `must be less than ${c.exclusiveMaximum}` };
      }
    }
    return { ok: true };
  }

  // Banner text after a save. Honest about restart: the engine reads config at
  // construction and this page cannot restart it.
  function savedMessage(result) {
    const n = ((result && result.applied) || []).length;
    const noun = n === 1 ? "setting" : "settings";
    if (result && result.needs_restart) {
      return `Saved ${n} ${noun}. Restart Poseidon for ${n === 1 ? "it" : "them"} to take effect.`;
    }
    return `Saved ${n} ${noun}.`;
  }

  // --- macro strip -----------------------------------------------------------

  // Overview strip cells from GET /api/macro. A missing leg renders an explicit
  // em-dash with an "unavailable" title — never 0, which would read as a real
  // measurement (a zero VIX means "no volatility", not "no data").
  function macroCells(data) {
    const d = data || {};
    const vix = d.vix;
    const spread = d.term_spread_10y_3m;
    const cells = [];
    cells.push(vix && vix.level != null
      ? { label: "VIX", value: Number(vix.level).toFixed(2),
          sub: `${d.vix_regime || ""} · delayed`, ok: true }
      : { label: "VIX", value: "—", sub: "unavailable", ok: false });
    cells.push(spread != null
      ? { label: "10Y-3M", value: `${(Number(spread) * 100).toFixed(2)}%`,
          sub: d.curve_inverted ? "inverted" : "normal", ok: true }
      : { label: "10Y-3M", value: "—", sub: "unavailable", ok: false });
    return cells;
  }

  // --- factor attribution ----------------------------------------------------

  // Rows for the backtest run card's factor block, or null when the backtest
  // ran without attribution (the key is present-but-null in that case).
  function factorRows(report) {
    const fa = report && report.factor_attribution;
    if (!fa) return null;
    const rows = [
      { label: "Alpha (annual)", value: `${(Number(fa.alpha_annual) * 100).toFixed(2)}%`,
        hint: "net of market, size and value" },
      { label: "t(alpha)", value: fa.t_alpha == null ? "—" : Number(fa.t_alpha).toFixed(2),
        hint: "|t| > 2 is the conventional bar" },
    ];
    const names = { mkt_rf: "Market", smb: "Size (SMB)", hml: "Value (HML)" };
    Object.keys(fa.loadings || {}).forEach((k) => {
      rows.push({ label: names[k] || k, value: Number(fa.loadings[k]).toFixed(3),
                  hint: "factor loading" });
    });
    rows.push({ label: "R²", value: fa.r2 == null ? "—" : Number(fa.r2).toFixed(3),
                hint: `${fa.n_days} days` });
    return rows;
  }

  const api = {
    groupSettings,
    sortWithinGroup,
    provenanceBadge,
    controlKind,
    statusNote,
    confirmMessage,
    coerce,
    withinConstraints,
    savedMessage,
    macroCells,
    factorRows,
    RISK_NOTE,
  };

  if (typeof window !== "undefined") window.PoseidonSettings = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
