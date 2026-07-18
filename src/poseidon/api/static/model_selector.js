/* Pure view-logic for the AI-brain (backend + model) selector (spec task 7).
 *
 * These are the load-bearing *pure* parts of the "AI brain" Account card — the
 * model-option list, the current-brain label, the paid/precondition note, the
 * VRAM sizing hint, and the custom-id-overrides-select choice. They live here
 * (not inline in app.js) so a node one-off unit test can exercise them without
 * a DOM: app.js is a plain top-level script and cannot be required under node,
 * whereas this IIFE guards its (nonexistent) browser hookup. All DOM wiring,
 * the paid confirm(), the precondition-disable, and the POST stay in app.js.
 *
 * Loaded before app.js, which reads window.PoseidonModelSelector. Under node
 * the module.exports hook feeds tests/frontend/model_selector.test.js.
 *
 * Backend ids mirror config.AIConfig.backend: "anthropic" (Claude API, paid) /
 * "openai_compatible" (local LM Studio, free). No secret ever reaches here —
 * GET /api/models returns only key_present/reachable booleans and model *ids*.
 */
"use strict";

(function () {
  // Q4_K_M weights ~= params_B * 0.6 GB + ~2 GB overhead, floored to a round 5B
  // tier; the next ~10B up is "tight". MUST match ai/hardware.vram_fit_hint so
  // the dashboard hint agrees with the server heuristic. Deliberately fuzzy —
  // never gates anything.
  function vramFitHint(totalGb) {
    const comfortable = Math.max(Math.floor((totalGb - 2) / 0.6 / 5) * 5, 0);
    const tight = comfortable + 10;
    return `~${comfortable}B (Q4) fit comfortably; ~${tight}B is tight.`;
  }

  // Full "#ai-vram-hint" string, or "" when it should be hidden: only for the
  // local backend, and only when GET /api/models returned a vram.total_gb.
  function vramHintText(backend, data) {
    if (backend !== "openai_compatible") return "";
    const vram = data && data.local && data.local.vram;
    if (!vram || vram.total_gb == null) return "";
    const gb = Number(vram.total_gb);
    return `Detected VRAM: ${gb} GB — models up to ${vramFitHint(gb)}`;
  }

  // Options for #ai-model-select given the chosen backend. anthropic → the
  // curated Claude ids; local → the probed LM Studio ids. The currently-active
  // model is always present and preselected even if it is not in the advertised
  // list (a custom/typo id, or Claude ids while running local) so the operator
  // never sees their live brain vanish from the dropdown.
  //   backend: "anthropic" | "openai_compatible"
  //   data:    GET /api/models body
  // → { options: [{ value, selected }], model } where model is the preselect.
  function modelOptions(backend, data) {
    const d = data || {};
    const cur = d.current_model || "";
    const curBackend = d.current_backend || "";
    const listed = backend === "anthropic"
      ? (d.anthropic && d.anthropic.models) || []
      : (d.local && d.local.models) || [];
    const values = listed.map((m) => String(m));
    // Preselect the live model only when the card's backend matches the running
    // one; otherwise default to the first advertised option (nothing selected
    // if the list is empty — the custom field is then the path).
    const preselect = backend === curBackend && cur
      ? cur
      : (values[0] || "");
    if (preselect && !values.includes(preselect)) values.unshift(preselect);
    return {
      model: preselect,
      options: values.map((v) => ({ value: v, selected: v === preselect })),
    };
  }

  // "#ai-current" label for the running brain: "Claude API · <model>" /
  // "Local · <model>".
  function currentLabel(data) {
    const d = data || {};
    const name = d.current_backend === "anthropic" ? "Claude API" : "Local";
    return `${name} · ${d.current_model || "—"}`;
  }

  // Precondition gate for the chosen backend. anthropic needs the vault key;
  // local needs a reachable endpoint. When blocked, Apply is disabled and the
  // note shown — this mirrors the server-side precondition in apply_ai_config
  // so the user is told *before* a POST that would 422.
  //   → { blocked, note } (note "" when not blocked)
  function precondition(backend, data) {
    const d = data || {};
    if (backend === "anthropic") {
      const ok = !!(d.anthropic && d.anthropic.key_present);
      return ok
        ? { blocked: false, note: "" }
        : { blocked: true, note: "Set your Anthropic API key in the vault first (Account view / poseidon vault set anthropic_api_key) before switching to the Claude API." };
    }
    const ok = !!(d.local && d.local.reachable);
    return ok
      ? { blocked: false, note: "" }
      : { blocked: true, note: "LM Studio not reachable — start it and load a model, then Refresh." };
  }

  // The model id to apply: a non-empty custom id overrides the dropdown, else
  // the select value. Trimmed; "" means "nothing chosen" (caller blocks Apply).
  function chosenModel(selectValue, customValue) {
    const custom = String(customValue || "").trim();
    if (custom) return custom;
    return String(selectValue || "").trim();
  }

  const api = {
    vramFitHint,
    vramHintText,
    modelOptions,
    currentLabel,
    precondition,
    chosenModel,
  };

  // Browser: expose for app.js. Node (unit test): export the pure helpers.
  if (typeof window !== "undefined") window.PoseidonModelSelector = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
