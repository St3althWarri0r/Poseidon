# Debug Console + Export-JSON + Model-Backend Health — Design

Date: 2026-07-17 · Branch: `feat/debug-console-health`

## Goal
Give an operator a one-glance, exportable timeline of what the dashboard did — which
button was pressed, which API call it made, and which server events resulted — plus a
clear signal (CLI probe + runtime notification) when the model backend is unreachable.

## Non-goals
- No new trading capability. The console is READ-ONLY observability (invariant: chat/UI
  can never place orders). It adds no state-changing endpoints and no auto-actions.
- No server-side capture/persistence of client activity; buffer is in-page memory only.
- No second websocket, no framework, no build step. Vanilla JS, matching `app.js`.
- Not a replacement for the existing `#events` bus feed (that stays as-is).

---

## Piece 1 — Debug Console (frontend)

### Files
- NEW `src/poseidon/api/static/debug.js` — self-contained IIFE, ~all logic.
- EDIT `index.html` — add `<script src="/static/debug.js?v=__V__"></script>` IMMEDIATELY
  BEFORE the existing `app.js` tag (line ~428) so the `fetch` wrapper is installed before
  `app.js` runs `route()`→`getJSON`→`fetch` and `connectWebsocket()`.
- EDIT `app.js` — ONE line only, inside `connectWebsocket()` `ws.onmessage` right after
  `evt` is parsed (line ~701): `window.__debugTap && window.__debugTap(evt);`
- EDIT `style.css` — append a `#dbg-*` block (panel, rows, kind badges).
- `debug.js` injects its own toggle button + panel DOM on load (keeps index.html to 1 line).

### State, toggle, persistence
- `DBG.on` boolean. Sources, in order: `?debug=1` in `location.search` → force on;
  else `localStorage.getItem("poseidon.debug") === "1"`. Default OFF.
- Always inject a small fixed-corner toggle button (`#dbg-toggle`, e.g. "🐞") so the
  feature is discoverable without the query param. Click toggles `DBG.on`, persists to
  localStorage, opens/closes the panel.
- OFF ⇒ ~zero overhead: every capture path's first statement is `if (!DBG.on) return …`
  (one boolean test per click/fetch/event). No cloning, timing, or DOM work when off.

### Ring buffer + entry schema
- `DBG.buf = []`, cap `MAX = 500`; `push(e){ buf.push(e); if(buf.length>MAX) buf.shift(); }`
- Monotonic `DBG.seq++` gives stable ordering / click→call→event correlation.
- Entry (common): `{ seq, ts: new Date().toISOString(), kind }` where kind ∈ `click|api|event`.
  - `click`: `{ …, id, label, view }` — button/anchor `id` (or `""`), visible label
    (`textContent` trimmed ≤80 chars, fallback `aria-label`/`title`), `view` = enclosing
    `.view[data-view]` dataset, else `"topbar"`/`"sidebar"`/`"modal"`.
  - `api`: `{ …, method, url, reqBody, status, ok, durationMs, resSummary, error }`.
  - `event`: `{ …, topic, payload }` (bounded, see tap).

### Source (a) — clicks
Single delegated listener: `document.addEventListener("click", onClick, true)` (capture,
so it sees clicks even when handlers `stopPropagation`). `onClick` returns if `!DBG.on`;
else `t = e.target.closest("button,a,[data-close-pos],[data-cancel],[data-approve],[data-reject],[data-algo],[data-broker]")`;
if `t`, push a `click` entry. Never reads form values (avoids capturing typed secrets).

### Source (b) — fetch wrapper (single chokepoint)
Install once at load: `const _fetch = window.fetch.bind(window); window.fetch = dbgFetch;`
`app.js` resolves `window.fetch` at call time, so `getJSON`/`postJSON`/`putJSON` and the
raw `fetch(...)` sites (app.js 1002/1074/1210) are all captured.
```
async function dbgFetch(input, init){
  if (!DBG.on) return _fetch(input, init);
  const method = (init?.method || "GET").toUpperCase();
  const url = redactUrl(String(input?.url || input));
  const reqBody = redactBody(init?.body);          // NEVER touch init.headers
  const t0 = performance.now();
  try {
    const res = await _fetch(input, init);
    const summary = await summarize(res);          // res.clone(); bounded+redacted
    push({kind:"api", method, url, reqBody, status:res.status, ok:res.ok,
          durationMs:Math.round(performance.now()-t0), resSummary:summary});
    return res;                                     // original returned untouched
  } catch (err) {
    push({kind:"api", method, url, reqBody, status:0, ok:false,
          durationMs:Math.round(performance.now()-t0), error:String(err)});
    throw err;                                       // never swallow
  }
}
```
`summarize(res)`: `await res.clone().text()`, slice to 2000 chars; if JSON-parseable,
`redactBody` the parsed object then re-stringify; the clone means the app still reads the
body normally.

### Redaction rule (LOAD-BEARING — invariant: secrets never logged)
- **Headers are never captured** (so `Authorization: Bearer …` / the dashboard token
  never enter the buffer).
- `redactUrl`: replace the value of any `token`/`key`/`secret` query param with `REDACTED`
  (covers `/ws?token=…` style and the SPA's `?token=`).
- `redactBody(x)`: if `x` is a JSON string parse it; deep-walk objects/arrays; for any key
  matching `SECRET_KEY = /(token|secret|passphrase|password|api[_-]?key|app[_-]?key|app[_-]?secret|refresh[_-]?token|credential|authorization)/i`
  replace its value with `"REDACTED"`. Non-JSON bodies are stored as `"[body N bytes]"`,
  never raw. Applied to BOTH request and response bodies (the response of
  `/api/brokers/schwab/exchange` contains `refresh_token`; the request of
  `/api/brokers/connect` contains broker creds — both must be redacted).

### Source (c) — /ws tap (no second socket)
`debug.js` defines `window.__debugTap = (evt) => { if (DBG.on) push({kind:"event",
topic: evt.topic, payload: bound(evt.payload)}); }` where `bound` deep-clones then caps
stringified size to ~2000 chars. `app.js`'s existing `ws.onmessage` calls it via the one
added guarded line, so the SAME already-open socket feeds the buffer. When `debug.js` is
absent or OFF the call is a no-op. This yields one correlated timeline: a `click`
(Run cycle) → its `api` (`POST /api/cycle`) → resulting `event`s (`ai.decision`,
`system.error`, `order.*`, `risk.*`).

### Render + Export
- Panel lists entries newest-first (`[...buf].reverse()`); re-render throttled via
  `requestAnimationFrame`, only while panel open. Each row: `ts` · kind badge (3 colors:
  click/api/event) · one-line summary; click a row to expand a `<pre>` of the full entry
  (`JSON.stringify(entry,null,2)`, escaped via the same `esc` idiom). Clear button empties
  the buffer.
- **Export JSON**: `#dbg-export` builds `new Blob([JSON.stringify(buf,null,2)],
  {type:"application/json"})`, `URL.createObjectURL`, a temp `<a download=
  "poseidon-debug-YYYYMMDD-HHMMSS.json">`, click, revoke. Fully client-side.

---

## Piece 2 — Backend-unreachable error subtype + runtime notification

### New error (core/errors.py)
```
class BackendUnreachableError(AgentError):
    """Model backend could not be reached (connect-phase failure), distinct from
    a model/schema/HTTP error. The server may return — honestly retryable."""
    retryable = True
```
Subclasses `AgentError` (⊂ `PoseidonError`); `retryable = True` is honest (a down LM
Studio can come back). Existing `except (AgentError, DataError)` still catches it.

### Classify connect failures (ai/backends/openai_backend.py)
In `complete`, split the current single `except (httpx.HTTPError, ValueError)` (line 72)
so the connect-phase subtypes come FIRST (they subclass `httpx.HTTPError`, so order
matters):
```
except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
    raise BackendUnreachableError(
        f"model backend unreachable at {self._client.base_url}: {exc}") from exc
except (httpx.HTTPError, ValueError) as exc:
    raise AgentError(f"local model backend error: {exc}") from exc
```
`httpx.ConnectError` is the source of the opaque "All connection attempts failed" string.
A `ReadTimeout` mid-generation or an HTTP 4xx/5xx stays a plain `AgentError` (server is up
but erroring — not "unreachable"). Symmetric change in `anthropic_backend.py:61`: reclassify
the existing `anthropic.APIConnectionError` branch to raise `BackendUnreachableError`.

### Tailored notification (app.py run_review_cycle)
Add a branch BEFORE the generic `except (AgentError, DataError)` (line 787), preserving the
graceful-degrade exactly (meter usage, publish, return — never re-raise):
```
except BackendUnreachableError as exc:
    log.error("model backend unreachable", error=str(exc))
    await self._record_ai_usage(self.agent.last_cycle_usage(), "failed",
                                cycle_id=f"failed-{uuid.uuid4().hex[:8]}")
    hint = (f"Model backend unreachable at {self.config.ai.base_url} — is LM Studio "
            "(or your model server) running?" if self.config.ai.backend ==
            "openai_compatible" else "Cannot reach the Anthropic API — check your network.")
    await self.bus.publish(Topics.SYSTEM_ERROR,
                           {"component": "model_backend", "error": hint})
    return
```
Reuses `notifications/service.py::_on_system_error` unchanged → title "Component error:
model_backend", body = the actionable hint, dedupe key `syserr:model_backend` (distinct
from `review_cycle`). No weakening of degrade; usage still metered.

---

## Piece 3 — `poseidon doctor` reachability probe

### Probe (cli.py, pure + injectable for tests)
```
def probe_model_backend(ai: AIConfig, api_key: str | None, *,
                        transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    if ai.backend == "openai_compatible":
        base = (ai.base_url or "").rstrip("/")
        try:
            with httpx.Client(timeout=5.0, transport=transport) as c:
                c.get(f"{base}/models").raise_for_status()
            return True, f"reachable at {base}"
        except httpx.HTTPError as exc:
            return False, f"UNREACHABLE at {base} — start LM Studio / the model server ({exc})"
    try:                                            # anthropic: cheap authed liveness
        with httpx.Client(timeout=5.0, transport=transport) as c:
            r = c.get("https://api.anthropic.com/v1/models",
                      headers={"x-api-key": api_key or "", "anthropic-version": "2023-06-01"})
        if r.status_code == 401:
            return False, "Anthropic API reachable but key rejected (401)"
        return True, "Anthropic API reachable"
    except httpx.HTTPError as exc:
        return False, f"cannot reach Anthropic API ({exc})"
```
`transport` lets tests inject `httpx.MockTransport` (a fake backend). GET `{base_url}/models`
is the natural LM Studio liveness endpoint (`base_url` already ends `/v1`).

### Wire into cmd_doctor
After the AI-key check (cli.py ~221), add:
`ok, detail = probe_model_backend(config.ai, key_or_None); check(f"model backend reachable ({config.ai.backend})", ok, detail)`
For anthropic pass the vault key (already unlocked in the vault block); for
openai_compatible pass `None`. Counts as a `problem` when unreachable — actionable and
correct for diagnostics.

---

## Security / safety checklist
- [ ] Read-only: console only wraps `fetch` to observe and taps the existing `/ws`; it
      issues no requests of its own and exposes no order/state-changing control.
- [ ] No new trade path: no button in the panel calls a state-changing endpoint.
- [ ] Secrets never logged: headers never captured; URL + request/response bodies run the
      `SECRET_KEY` redaction walk; non-JSON bodies stored as size only.
- [ ] Off by default: gated by `?debug=1` / localStorage; OFF ⇒ one boolean test, no
      behavior change; the added `app.js` line is a no-op without `debug.js` / when OFF.
- [ ] Bounded: 500-entry ring, ≤2000-char body summaries, capped rendered rows.
- [ ] Money stays Decimal (backend untouched here); errors subclass `PoseidonError`,
      `retryable` honest.

## TDD task list (ordered; backend first — it is unit-testable)
1. `core/errors.py`: add `BackendUnreachableError(AgentError)`. Test: subclass of
   `AgentError`/`PoseidonError`, `retryable is True`.
2. `openai_backend.py`: connect-phase classification. Test (MockTransport, mirror
   `test_backend_openai.py`): handler raising `httpx.ConnectError` ⇒ `BackendUnreachableError`;
   HTTP 500 and non-object JSON ⇒ plain `AgentError` (NOT the subclass).
3. `anthropic_backend.py`: reclassify `APIConnectionError` ⇒ `BackendUnreachableError`.
   Test: fake client raising `anthropic.APIConnectionError`.
4. `app.py run_review_cycle`: new `except BackendUnreachableError` before the generic one.
   Test with a stub agent raising it: (a) usage metered, (b) `SYSTEM_ERROR` published with
   `component=="model_backend"` and `base_url` in `error`, (c) cycle returns cleanly;
   regression: plain `AgentError` still publishes `component=="review_cycle"`.
5. `cli.py`: `probe_model_backend` + wire into `cmd_doctor`. Test (MockTransport):
   200 ⇒ `(True, …)`; `ConnectError` ⇒ `(False, hint containing base_url and "LM Studio")`;
   anthropic 401 ⇒ `(False, "…key rejected…")`.
6. `debug.js`: implement ring buffer, `redactUrl`, `redactBody`, `summarize`, click
   listener, `dbgFetch`, `__debugTap`, render, export. Keep `redactBody`/`redactUrl` as
   pure top-level functions.
7. `index.html` (script tag before app.js) + `app.js` (one tap line) + `style.css` (`#dbg-*`).
8. Frontend tests: no JS harness exists in-repo, so — (a) unit-test the pure redaction
   logic if a `node` one-off is acceptable (assert a body with `refresh_token`/`app_secret`
   ⇒ `REDACTED`; a `?token=x` URL ⇒ redacted); (b) documented manual check: `?debug=1`,
   press HALT→Cancel, Run cycle, Connect-broker with fake creds, then Export JSON and
   grep the file — assert no token/secret/passphrase value appears and the
   click→api→event ordering is correct.

## Existing tests that may change / need adding
- `tests/unit/test_backend_openai.py` — 500⇒`AgentError` assertion still passes
  (`BackendUnreachableError` ⊂ `AgentError`); ADD a ConnectError case + assert 500 is not
  the subclass. No breakage expected.
- `tests/unit/test_backend_anthropic.py` — update if it asserts `APIConnectionError` maps
  to bare `AgentError`; it now maps to the subclass (still an `AgentError`).
- `tests/unit/test_local_model_robustness.py` — unaffected (its cases are non-connect).
- Doctor: add coverage (no dedicated `test_cli_doctor` exists today).
- `app.py` review-cycle tests (if any assert the `review_cycle` component/degrade path) —
  confirm the new branch doesn't change the generic path.
