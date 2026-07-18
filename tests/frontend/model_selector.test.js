/* Pure-function unit test for the AI-brain selector view-logic (spec task 7).
 * No JS test harness in-repo, so this is a node one-off that requires
 * model_selector.js (its browser hookup is a no-op under node, leaving only the
 * pure helpers) and asserts the load-bearing UI invariants:
 *   - the model dropdown lists the right ids per backend and always keeps the
 *     live model selectable (even a custom/typo id, or Claude ids while local);
 *   - the VRAM hint matches the server heuristic and is local-only;
 *   - the precondition note/disable fires for a missing key / unreachable local
 *     so the operator is warned BEFORE a POST that would 422;
 *   - a non-empty custom id overrides the select on Apply.
 * Run directly (`node model_selector.test.js`) or via the pytest wrapper
 * tests/unit/test_model_selector_frontend.py. Exit 0 = pass.
 */
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const { vramFitHint, vramHintText, modelOptions, currentLabel, precondition, chosenModel } = require(
  path.join(__dirname, "..", "..", "src", "poseidon", "api", "static", "model_selector.js")
);

// Representative GET /api/models body: running local, both backends usable.
const local = {
  current_backend: "openai_compatible",
  current_model: "openai/gpt-oss-20b",
  anthropic: { models: ["claude-opus-4-8", "claude-haiku-4-5-20251001"], key_present: true },
  local: { reachable: true, models: ["openai/gpt-oss-20b", "qwen/qwen3-coder-30b"], vram: { total_gb: 16.0, free_gb: 11.2 } },
};

/* ---- vramFitHint: MUST match ai/hardware.vram_fit_hint ---- */
assert.equal(vramFitHint(16.0), "~20B (Q4) fit comfortably; ~30B is tight.", "16 GB heuristic");
assert.equal(vramFitHint(8.0), "~10B (Q4) fit comfortably; ~20B is tight.", "8 GB heuristic");
assert.equal(vramFitHint(2.0), "~0B (Q4) fit comfortably; ~10B is tight.", "tiny VRAM floors at 0");

/* ---- vramHintText: local-only, hidden without a total ---- */
assert.equal(vramHintText("openai_compatible", local),
  "Detected VRAM: 16 GB — models up to ~20B (Q4) fit comfortably; ~30B is tight.", "local hint text");
assert.equal(vramHintText("anthropic", local), "", "no VRAM hint on the Claude backend");
assert.equal(vramHintText("openai_compatible", { local: { reachable: false, models: [], vram: null } }),
  "", "no VRAM hint when detection returned null");

/* ---- modelOptions: right list per backend, live model kept ---- */
{
  // Local backend active → local ids, running model preselected.
  const m = modelOptions("openai_compatible", local);
  assert.deepEqual(m.options.map((o) => o.value), ["openai/gpt-oss-20b", "qwen/qwen3-coder-30b"]);
  assert.equal(m.model, "openai/gpt-oss-20b");
  assert.equal(m.options[0].selected, true, "running local model preselected");
  assert.equal(m.options[1].selected, false);
}
{
  // Switching the card to anthropic while running local → curated Claude ids,
  // first one preselected (the live local model does not belong to this list).
  const m = modelOptions("anthropic", local);
  assert.deepEqual(m.options.map((o) => o.value), ["claude-opus-4-8", "claude-haiku-4-5-20251001"]);
  assert.equal(m.options[0].selected, true, "first Claude id preselected on cross-backend view");
}
{
  // A live custom/typo local id NOT in the probe list is still offered + selected.
  const custom = { ...local, current_model: "my/custom-model-v9" };
  const m = modelOptions("openai_compatible", custom);
  assert.ok(m.options.some((o) => o.value === "my/custom-model-v9" && o.selected),
    "live custom id kept selectable even when unadvertised");
  assert.equal(m.options[0].value, "my/custom-model-v9", "and surfaced first");
}
{
  // Empty local list (unreachable) → no options, empty preselect (custom path).
  const m = modelOptions("openai_compatible", { current_backend: "anthropic", current_model: "claude-opus-4-8", local: { reachable: false, models: [], vram: null } });
  assert.deepEqual(m.options, []);
  assert.equal(m.model, "");
}

/* ---- currentLabel ---- */
assert.equal(currentLabel(local), "Local · openai/gpt-oss-20b");
assert.equal(currentLabel({ current_backend: "anthropic", current_model: "claude-opus-4-8" }), "Claude API · claude-opus-4-8");
assert.equal(currentLabel({}), "Local · —");

/* ---- precondition: key/reachable gate ---- */
assert.deepEqual(precondition("anthropic", local), { blocked: false, note: "" }, "key present → allowed");
assert.equal(precondition("openai_compatible", local).blocked, false, "reachable → allowed");
{
  const noKey = { ...local, anthropic: { models: ["claude-opus-4-8"], key_present: false } };
  const p = precondition("anthropic", noKey);
  assert.equal(p.blocked, true, "missing key blocks Apply-to-Claude");
  assert.ok(/vault/i.test(p.note), "note tells the operator to set the vault key");
}
{
  const down = { ...local, local: { reachable: false, models: [], vram: null } };
  const p = precondition("openai_compatible", down);
  assert.equal(p.blocked, true, "unreachable blocks Apply-to-local");
  assert.ok(/reachable/i.test(p.note), "note explains LM Studio is down");
}

/* ---- chosenModel: custom overrides the select ---- */
assert.equal(chosenModel("openai/gpt-oss-20b", ""), "openai/gpt-oss-20b", "select used when no custom id");
assert.equal(chosenModel("openai/gpt-oss-20b", "  my/other  "), "my/other", "custom id overrides + trims");
assert.equal(chosenModel("", ""), "", "nothing chosen → empty (caller blocks Apply)");

console.log("model_selector.test.js: all assertions passed");
