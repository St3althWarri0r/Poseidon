# Debug Console (read-only observability)

The debug console is an in-page, **read-only** timeline of what the dashboard did:
which button was pressed (`click`), which API call it made (`api`), and which `/ws`
events resulted (`event`). It issues no requests of its own, exposes no
order/state-changing control, and is **OFF by default**. When off, every capture
path is a single boolean test — no behavior change.

## Turning it on

- Append `?debug=1` to the dashboard URL, **or**
- Click the fixed-corner 🐞 toggle (bottom-right). The choice persists in
  `localStorage` under `poseidon.debug`.

Open the panel with the toggle. Each row is `time · kind badge · one-line summary`;
click a row to expand the full JSON. **Export JSON** downloads the whole 500-entry
ring buffer as `poseidon-debug-YYYYMMDD-HHMMSS.json`; **Clear** empties it.

## Secret-redaction invariant (LOAD-BEARING)

Secrets must **never** enter the buffer or the export:

- Request **headers are never captured** (so `Authorization: Bearer …` and the
  dashboard token never appear).
- URL query params named like a secret (`token`/`key`/`secret`/…) have their value
  replaced with `REDACTED`.
- Request/response bodies are deep-walked; any object key matching
  `token|secret|passphrase|password|api_key|app_key|app_secret|refresh_token|credential|authorization`
  (case-insensitive) has its value replaced with `REDACTED`. Non-JSON bodies are
  stored as `"[body N bytes]"` — never raw.

The pure redaction functions (`redactUrl`, `redactBody`, `walk`) are unit-tested:
`tests/frontend/debug_redaction.test.js`, run inside the pytest gate via
`tests/unit/test_debug_redaction.py` (skipped if `node` is absent).

## Documented manual export check (spec task 8b)

Run this after any change to `debug.js` or the capture wiring to confirm, in the
real browser, that (1) no secret value ever lands in the export and (2) the
`click → api → event` correlation is intact.

1. Start the dashboard and open it with `?debug=1`. Confirm the 🐞 toggle shows
   "on" and the panel opens.
2. Exercise the three sources, generating at least one entry each:
   - **HALT → Cancel**: click the HALT control, then Cancel in the confirm dialog
     (no state change — just captures two `click` entries).
   - **Run cycle**: click Run cycle. Expect a `click`, then its `api`
     (`POST /api/cycle`), then resulting `event`s (`ai.decision`, `order.*`,
     `risk.*`, or `system.error`).
   - **Connect broker with FAKE creds**: open Connect broker, type obviously fake
     but secret-shaped values (e.g. app key `FAKEKEY123`, app secret
     `FAKESECRET456`, any token/passphrase field), and submit. This drives the
     `api` body-redaction path (`/api/brokers/connect` request carries creds; a
     schwab exchange response carries `refresh_token`).
3. Click **Export JSON** and open the downloaded file, then grep it:

   ```sh
   # from your Downloads dir — assert NO secret VALUE survived:
   grep -Ei 'FAKEKEY123|FAKESECRET456|<any token/passphrase you typed>' poseidon-debug-*.json
   # → expect NO matches. You SHOULD see "REDACTED" and "[body N bytes]" instead:
   grep -c 'REDACTED' poseidon-debug-*.json
   ```

   Also confirm no `Authorization`/`Bearer` header value and no `?token=<value>`
   appears anywhere in the file.
4. Confirm ordering: entries carry a monotonic `seq`. For Run cycle, the `click`
   entry's `seq` precedes its `api` entry's `seq`, which precedes the resulting
   `event` entries — one correlated timeline.

**Pass criteria:** the export contains `REDACTED`/`[body N bytes]` placeholders,
**zero** secret values, no captured header/token, and a correct
`click → api → event` `seq` ordering.
