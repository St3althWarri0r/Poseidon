# Poseidon — Dashboard AI Backend + Model Selector (design spec)

**Goal.** One dashboard control that (a) switches Poseidon's portfolio-manager brain between
**Claude API** (`backend: anthropic`, paid) and **Local LM Studio** (`backend: openai_compatible`,
free), (b) picks the model within whichever is selected, and (c) **auto-writes the config overlay** so
the operator never edits YAML. Kills the friction where `ai.model` must exactly match the model loaded
in LM Studio, and makes Poseidon portable across machines/models. Config-only: no order path, no mode
change, no secret handling.

**Non-goals (future work).** Hardware scan / auto-install of models; auto-download; per-role tiering UI
(the existing `ai.utility_model` stays a YAML-only advanced knob); a Claude *pricing* editor
(`input/output_price_per_mtok` stay in `poseidon.yaml`). VRAM detection is a *hint only* — no enforcement.

## Corrections to the verified context (checked against code)

1. **`ai.base_url` defaults to `None`** (`config.py:102`), not `http://localhost:1234/v1`. The
   `AIConfig` validator *requires* `base_url` when `backend == "openai_compatible"` (`:124`). So when the
   selector switches to Local it MUST supply a base_url if none is set → use module constant
   `DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"`. GET `/api/models` also probes
   `ai.base_url or DEFAULT_LM_STUDIO_URL` (base_url is `None` while running on Claude).
2. **The reviewer is NOT wired in `_wire_ai`.** It is `review_algorithm(self._backend, …)` called
   inline (`app.py:875-880`) reading `self._backend` fresh each call → a live rebind of the kernel attr
   is automatically seen; no reviewer setter needed.
3. **Reflection & analysis auto-follow.** Both take `get_backend=lambda: self._utility_backend`
   (`app.py:251,262`) → they resolve the utility backend at call time. Only **agent** (`ClaudeAgent`,
   `app.py:247`) and **chat** (`ChatService`, `:271`) hold a *frozen* `self._backend` ref set in their
   ctors — these are the only two objects needing an explicit rebind on a live swap.
4. **`AIConfig` is a mutable `StrictModel`** (`extra="forbid"`, not frozen) → reassign via
   `self.config.ai = self.config.ai.model_copy(update=…)`. The `ai:` block is NOT special-cased in
   `apply_local_overlay` (only `brokers`/`data.providers` are) → it deep-merges cleanly: overlay `ai`
   keys win, base `ai` keys (effort, prices, reflection…) survive.
5. **Canonical Claude ids in-repo:** only `claude-opus-4-8` and `claude-haiku-4-5-20251001`
   (`docs/api-configuration.md`). Do NOT invent `claude-sonnet-5`. `CURATED_CLAUDE_MODELS` is a code
   constant seeded from these; every id MUST support adaptive thinking + `effort` (the anthropic backend
   always sends `thinking={"type":"adaptive"}`+`output_config={"effort"}` when `force_tool is None`,
   `anthropic_backend.py:50-52`) or the API 400s. Extend it as Anthropic ships adaptive-capable ids
   (verify via the `claude-api` skill).

## Endpoint 1 — `GET /api/models`

Read-only assembly (mirror `GET /api/brokers`, `server.py:543`). Never raises; degrades to reachable
`false` / `null`.

```json
{ "current_backend": "openai_compatible", "current_model": "openai/gpt-oss-20b",
  "anthropic": { "models": ["claude-opus-4-8","claude-haiku-4-5-20251001"], "key_present": true },
  "local": { "reachable": true, "models": ["openai/gpt-oss-20b","qwen/qwen3-coder-30b"],
             "vram": { "total_gb": 16.0, "free_gb": 11.2 } } }
```

- `current_backend/current_model` ← `kernel.config.ai.backend` / `.model`.
- `anthropic.models` ← `CURATED_CLAUDE_MODELS` constant. `key_present` ← `ai.api_key_credential in
  kernel.vault.names()` (suppress `VaultLockedError` → `false`).
- `local.reachable/models` ← `probe_local_models(ai.base_url or DEFAULT_LM_STUDIO_URL)` (see below);
  on any error → `false`, `[]`.
- `local.vram` ← `detect_vram()` (best-effort; `null` when no GPU tool / parse fails).

## Endpoint 2 — `POST /api/models` (apply)

Mirror `POST /api/brokers/connect` (`server.py:616`). Body `{ "backend": "...", "model": "..." }`.

- Validate: `backend in {"anthropic","openai_compatible"}`; `model` a non-empty stripped string
  (custom ids allowed — a wrong local id surfaces on the next cycle, see Failure modes). Bad body → 422.
- `result = await kernel.apply_ai_config(backend=backend, model=model)`.
- `except (ConfigError, VaultError, DataError, AgentError) as exc: raise HTTPException(422, str(exc))`.
- Return `{ "ok": true, "ai": { "backend","model","base_url","paid" } }` (`paid = backend=="anthropic"`).

## Kernel apply mechanism — `ApplicationKernel.apply_ai_config(backend, model)`

Lives in `app.py` beside `switch_broker`. **Decision: live swap (preferred), not restart** — the seam
supports it cleanly for the two frozen refs, and the openai model string is per-request so most swaps are
near-free. Restart-fallback is unnecessary and is explicitly rejected.

Ordered steps:

1. **Build target cfg.** `base = self.config.ai`. `base_url = base.base_url or DEFAULT_LM_STUDIO_URL`
   when target is local (else keep `base.base_url`). `target = base.model_copy(update={"backend":
   backend, "model": model, "base_url": base_url})`. **Backend-change guard:** if `backend !=
   base.backend`, also `update utility_model=None` (a stale cross-backend utility id would break the new
   backend; utility then follows primary). Constructing `target` runs the `AIConfig` validator early.
2. **Preconditions (before any swap — prove, then commit, like the broker test):**
   - target `anthropic`: `if base.api_key_credential not in self.vault.names(): raise
     ConfigError("Set your Anthropic API key in the vault first (Account view / poseidon vault set
     anthropic_api_key) before switching to the Claude API.")` — this runs *before* `build_backends`,
     which would otherwise raise a raw `VaultError` from `vault.get`.
   - target `openai_compatible`: `reachable, _ = await probe_local_models(base_url)`;
     `if not reachable: raise ConfigError(f"LM Studio not reachable at {base_url} — start it and load a
     model, then retry.")` — never switch into a dead backend.
3. **Cycle-lock swap.** `async with self._cycle_lock:` (awaits out any running cycle — a multi-round tool
   loop holds this for its whole duration, so the swap cannot land between rounds; the in-flight decision
   finishes entirely on its original backend/model). Inside:
   - `old_primary, old_utility = self._backend, self._utility_backend`.
   - `new_primary, new_utility = build_backends(target, self.vault.get)`.
   - Rebind: `self._backend = new_primary`; `self.agent.rebind_backend(new_primary)`;
     `self._utility_backend = new_utility`; `self.chat.rebind_backend(new_utility)`;
     `self.config.ai = target`. (Reviewer + reflection + analysis auto-follow — see seam note.)
   - `await asyncio.to_thread(self._write_ai_overlay, target)` (persist).
   - `await self.audit.append("human", "ai.backend_changed", {"backend": backend, "model": model,
     "paid": backend=="anthropic"})`.
4. **Close old backends** (after releasing the lock), mirroring shutdown (`app.py:1253-1260`):
   `for b in (old_primary, old_utility): if b is not new_primary and b is not new_utility: suppress →
   await b.aclose()` (distinct-object guard avoids double-close in the untiered shared-object case).
5. **Notify** (never changes mode): `bus.publish(Topics.NOTIFY, {"level": "warning" if paid else "info",
   "title": f"AI brain: {'Claude API' if paid else 'Local'} · {model}", "body": …cost line…})`.
6. Return `{"backend","model","base_url","paid"}`.

**Seam note — why not re-call `_wire_ai`.** `_wire_ai` re-subscribes `self.reflection.on_account_synced`
to `Topics.ACCOUNT_SYNCED` (`app.py:256`); calling it twice double-subscribes → double reflection runs.
It also reconstructs agent/chat, which needs the `dispatcher`/`chat_dispatcher` (local vars in `start()`,
not stored on `self`). So the live path deliberately **rebinds** instead: kernel attrs + two new 1-line setters
(`ClaudeAgent.rebind_backend` / `ChatService.rebind_backend`, each `self._backend = b`) — the only
non-additive touchpoints. This is the one place the seam does not support a wholesale re-wire.

## Config-overlay persistence — `_write_ai_overlay(cfg: AIConfig)`

Mirror `_write_broker_overlay` (`app.py:524`): read existing `poseidon.local.yaml` (parse-error →
`ConfigError`), set its `ai` sub-block, atomic tmp-write, same "SECRETS NEVER STORED HERE" header. Write
only `{"backend", "model", "base_url"}` (+ `"utility_model": None` **only** when a backend change cleared
it — explicit null so `_deep_merge` overrides a base value). base config keys survive the startup
`apply_local_overlay` deep-merge. No secrets ever (only `api_key_credential` *name*, already in base
config). On restart, `load_config` re-applies the overlay → the choice persists.

## VRAM detection + heuristic — `ai/hardware.py` (new, dependency-free)

- `async def detect_vram() -> dict|None`: `asyncio.to_thread` →
  `nvidia-smi --query-gpu=memory.total,memory.free --format=csv,noheader,nounits` (MiB → GB); fallback
  `rocm-smi --showmeminfo vram`. Return `{"total_gb","free_gb"}` (free omitted if unavailable). `None` on
  FileNotFoundError / nonzero exit / parse failure / timeout (~2s). Never raises.
- `def vram_fit_hint(total_gb: float) -> str`: Q4_K_M ≈ `params_B*0.6 GB` weights + ~2 GB overhead →
  `fits_B = round((total_gb - 2) / 0.6)`. e.g. 16 GB → "~20B (Q4) fit comfortably; ~30B is tight." Copy
  is explicitly approximate.

## Local model probe — `probe_local_models(base_url, *, transport=None) -> tuple[bool, list[str]]`

Module-level helper (in `ai/hardware.py` or `ai/backends/__init__.py`) so it works even when the local
backend is not the active one. Short-timeout `httpx.AsyncClient` (`transport` injectable for tests) →
`GET {base_url}/models` → `(True, [m["id"] for m in data.get("data", [])])`. Any
`httpx.HTTPError`/`ValueError`/shape error → `(False, [])`. Reused by GET `/api/models` and step 2 above.

## UI — new "AI brain" card in the Account view

Add a card row in `index.html` under the Account `<section data-view="account">` (`:207`), styled like the
broker card (reuse `.warn-note`, `.meter-note`, `.ticket-actions`). Element ids:

- **Backend toggle** — two buttons/radios: `#ai-backend-anthropic` ("Claude API") / `#ai-backend-local`
  ("Local · LM Studio"). Selecting one repopulates the model select + toggles the hints below.
- **Model select** `#ai-model-select` — anthropic → `anthropic.models`; local → `local.models`
  (current model preselected, shown even if not in list). **Custom id** `#ai-model-custom` text input
  ("or type a model id") — non-empty value overrides the select on Apply (mainly local; allowed for
  anthropic with the adaptive-thinking caveat in a small note).
- **Current** `#ai-current` — e.g. "Claude API · claude-opus-4-8" / "Local · openai/gpt-oss-20b".
- **VRAM hint** `#ai-vram-hint` — local only, hidden when `vram==null` or backend=anthropic:
  "Detected VRAM: 16 GB — models up to ~20B (Q4) fit comfortably; ~30B is tight." (from `vram_fit_hint`).
- **Cost note** `#ai-cost` (`.warn-note`) — shown when anthropic selected: "Claude API is billed per
  token; the local model is free. Switching does not change your operating mode."
- **Precondition note** `#ai-precond-warning` (`.warn-note`) — anthropic + `!key_present` → "Set your
  Anthropic API key in the vault first."; local + `!reachable` → "LM Studio not reachable at {base_url}."
  Either case disables `#ai-apply`.
- **Buttons** `#ai-apply` ("Apply"), `#ai-refresh` ("Refresh").

app.js (vanilla, mirror `refreshAccount`/broker handlers): `async function refreshModels()` →
`getJSON("/api/models")`, render; wire into the view registry so
`account.refresh = Promise.allSettled([refreshStatus(), refreshAccount(), refreshModels()])`. `#ai-apply`
handler: build `{backend, model}` (custom overrides select); if switching to Claude API,
`window.confirm("Switch to the paid Claude API? …")` (analogous to the LIVE-broker confirm,
`app.js:1487`); `postJSON("/api/models", body)`; `toast(...)`; `refreshModels()` + `refreshStatus()`.
`#ai-refresh` → `refreshModels()`.

## Failure modes

- **LM Studio unreachable** → GET: `local.reachable=false, models:[]`; UI shows the note + disables
  Apply-to-local. POST to local re-probes and 422s ("not reachable") — never switches into a dead backend.
- **Anthropic key missing** → GET: `key_present=false`; UI note + disabled Apply. POST double-checks
  `vault.names()` and 422s before any swap (no half-switch).
- **Invalid/typo local model id** → accepted (non-empty); applied; the model-not-found surfaces on the
  first cycle as `AgentError` → existing `run_review_cycle` "review cycle failed" degrade
  (`app.py:787-795`, no order, metered, notified). Not re-solved here; not regressed.
- **GPU tool absent** → `detect_vram()` returns `null`; VRAM hint hidden.
- **Backend build/validation fails under the lock** (bad base_url, anthropic client init) → exception
  before any rebind → caught → 422; old backends retained and still active.

## Safety checklist / invariants (preserve)

- **Config-only.** Touches `ai.*` + the overlay + backend objects; no `OrderManager`/`Broker`/risk path.
- **No mid-cycle swap.** Apply awaits `_cycle_lock`; the running decision finishes on its original brain.
- **Primary-model correctness.** Trading agent + reviewer always use the selected primary
  (`self._backend`); untiered → chat/reflection share it. Weak-model risk stays covered by existing
  degrade-to-no_action / voided-decision robustness — not re-solved, not regressed.
- **Secrets untouched.** Only credential *names* referenced (`api_key_credential`); overlay never holds a
  value; vault is read via `names()`/`get` exactly as today.
- **No mode change.** Switching backend is a *spend* change (surfaced), never a real-money/mode change.

## Ordered TDD task list (backend unit-testable with fake vault/backends/transport)

1. **`_write_ai_overlay`** (app.py). Test: writes `ai:{backend,base_url,model}` → round-trips through
   `apply_local_overlay` + `load_config` to the expected `AIConfig`; preserves a pre-existing `brokers`
   overlay; no secret value written; `utility_model:null` emitted only on a backend change.
2. **`rebind_backend`** on `ClaudeAgent` + `ChatService`. Test: after rebind, `.run_cycle`/`.send` drive
   the *new* fake backend (call recorded on new, not old).
3. **`probe_local_models`** + **`detect_vram`/`vram_fit_hint`** (ai/hardware.py). Tests (fake transport /
   canned csv): reachable → id list; HTTP error → `(False, [])`; nvidia-smi parse → dict; binary absent →
   `None`; heuristic number for 16 GB.
4. **`apply_ai_config`** (app.py). Tests with fake vault + fake backends + fake transport:
   (a) same-backend model change → agent+reviewer see new model, overlay written;
   (b) anthropic→local → agent+chat rebound, reflection/analysis follow via lambda, old backends
   `aclose`d, `utility_model` cleared;
   (c) anthropic target, key absent → `ConfigError`, no swap, backends unchanged;
   (d) local target unreachable → `ConfigError`, no swap;
   (e) swap awaits a held `_cycle_lock` (running cycle finishes on old model, then swap applies);
   (f) audit row appended; notify published; mode unchanged.
5. **`GET /api/models`** route. Tests (fake kernel + transport): reachable path shape; unreachable →
   `reachable:false, models:[]`; `key_present` reflects `vault.names()`; `vram` null when tool absent.
6. **`POST /api/models`** route. Tests: happy path calls `apply_ai_config` and echoes `ai`; missing-key →
   422; bad backend / empty model → 422.
7. **Frontend** (lighter; smoke/manual): index.html card + app.js `refreshModels` + toggle/select/custom/
   apply/refresh handlers + paid-confirm + precondition-disable; wired into `account.refresh`.

## Existing tests — expected impact

Additive; nothing should break. Likely *extended* (not changed): `test_backend_tiering.py`
(`build_backends`), `test_config_ai_backend.py` (`AIConfig` + overlay round-trip),
`test_local_backend_cycle.py`. `_wire_ai` is never re-called, so wiring/construction tests are untouched;
shutdown's distinct-object `aclose` guard is reused, not modified. New unit files:
`test_ai_model_selector.py` (apply_ai_config), `test_hardware_vram.py`, `test_api_models.py`.
