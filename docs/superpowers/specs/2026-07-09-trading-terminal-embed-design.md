# Trading Terminal embed — design

**Date:** 2026-07-09
**Status:** Approved (user, 2026-07-09)
**Repos:** `St3althWarri0r/Poseidon` (host) and `St3althWarri0r/Trading-Terminal` (source of the UI)

## Goal

Embed the entire Trading-Terminal market-study app (Bloomberg-style cockpit:
command bar, market monitor, charts, fundamentals, news, watchlist) into
Poseidon so it is served by the Poseidon process itself — one service, one
port, a "Terminal" entry in the dashboard nav. Poseidon stays pure Python at
runtime.

## Decisions (settled with the user)

1. **Full embed, pure Python.** The terminal UI ships as a prebuilt static
   bundle served by FastAPI; its six data endpoints are reimplemented in
   Python. Rejected: Node sidecar service (second runtime + service to
   manage); vanilla-JS rewrite (weeks of work, permanent fork).
2. **Pure mirror v1.** Keyless Yahoo data, identical behavior to the
   standalone terminal. No coupling to brokers, the kernel, the risk engine,
   or Poseidon's strict-freshness data router. Portfolio overlay (e.g. a
   `PORT` function code reading `/api/portfolio`) is an explicit phase-2
   candidate, out of scope here.

## Architecture

```
Browser ── /terminal            → FastAPI StaticFiles (prebuilt React bundle)
        └─ /api/terminal/*      → poseidon.terminal.routes (APIRouter)
                                   └─ poseidon.terminal.yahoo (httpx + TTL cache)
                                       └─ public Yahoo Finance endpoints (keyless)
```

### Poseidon side (new code)

New isolated package `src/poseidon/terminal/`:

- **`yahoo.py`** — keyless async Yahoo client on `httpx` (already a
  dependency; no new deps). Replicates the standalone terminal's
  `lib/yahoo.ts`: in-memory TTL cache (10 s quotes, 30 s intraday chart /
  120 s longer ranges, 30 s news, 6 h fundamentals, 15 s market overview,
  60 s search; ~512-entry opportunistic eviction), symbol sanitization
  (`[A-Z0-9.^=-]`, ≤20 chars), and the normalization quirks that were
  review-verified in the standalone app: Yahoo quote `dividendYield` and
  `debtToEquity` are percents → stored as fractions; candles with null
  O/H/L/C dropped; candle timestamps sorted ascending and deduped
  (last-write-wins); sector list sorted by change desc with nulls last.
  Uses the same public endpoints `yahoo-finance2` v3 uses — chart v8 and
  search v1 (no auth), quote v7 and quoteSummary v10 (cookie + crumb
  handshake: fetch cookie, then `/v1/test/getcrumb`, cache both, refresh on
  401/403 once). Exact params verified against the vendored
  `yahoo-finance2` source during implementation.
- **`routes.py`** — `APIRouter` exposing, under **`/api/terminal`**:
  `GET /quote?symbols=A,B`, `GET /chart?symbol=&range=`, `GET /search?q=`,
  `GET /fundamentals?symbol=`, `GET /news[?symbol=]`, `GET /market` — all
  query-param shaped, matching the standalone `app/api/*/route.ts` handlers
  (which read `req.nextUrl.searchParams`), so the React client works
  unmodified apart from its base-path constant.
  Response bodies must match the standalone repo's `lib/types.ts` shapes
  field-for-field (the React client is the contract). Errors return the
  terminal's envelope `{"error": "<msg>"}` with 4xx/5xx status (502 for
  upstream failure) so the React error states render unchanged. Endpoint
  paths/param shapes follow the standalone `app/api/*` routes exactly except
  for the `/api/terminal` prefix; `lib/api-client.ts` is parameterized to
  point here (see below).
- **`constants.py`** — mirror of the standalone `lib/constants.ts`: market
  monitor universes (indices, futures, rates, commodities, crypto, FX,
  sector ETFs) and the range→interval/period config.

Wiring (the only touches outside the new package):

- `api/server.py`: `include_router(terminal_router)` and mount the bundle —
  `app.mount("/terminal", StaticFiles(directory=STATIC_DIR / "terminal", html=True))`.
- `api/static/index.html`: a "Terminal" nav entry (styled like the existing
  `data-view` items) that navigates to `/terminal` (full page — the terminal
  is a full-screen cockpit, not a dashboard panel).
- The namespace deliberately avoids Poseidon's existing `/api/quote/{symbol}`
  (trade-ticket semantics, fresh-only) — study data and trading data stay
  separate systems.

### Bundle (committed artifact)

`src/poseidon/api/static/terminal/` holds the built React app (index.html +
`_next/` assets, ~0.5–1 MB). It ships automatically: hatchling packages
everything under `src/poseidon`, and the PKGBUILD wraps the wheel. The bundle
is regenerated only when the Trading-Terminal repo changes, via the workflow
below; a `README.md` inside the bundle dir records the source repo, commit,
and regeneration command.

### Trading-Terminal side (non-breaking, standalone behavior unchanged)

- `lib/api-client.ts`: API base becomes
  `const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api"` (inlined at
  build time; standalone builds are byte-identical in behavior).
- `next.config.ts`: when `TERMINAL_EMBED=1` → `output: "export"`,
  `basePath: "/terminal"`.
- `package.json`: `build:embed` script — temporarily excludes `app/api/**`
  (route handlers are incompatible with `output: "export"`; the script moves
  the dir aside and restores it via shell trap), builds with
  `TERMINAL_EMBED=1 NEXT_PUBLIC_API_BASE=/api/terminal`, and when
  `EMBED_DEST` is set syncs `out/` there (the documented value is
  `Poseidon/src/poseidon/api/static/terminal/`); without `EMBED_DEST` it
  just builds and prints the `out/` path.
- Docs: README section "Embedding in Poseidon".

## Data flow

1. Browser loads `/terminal` → static React app (basePath-aware assets under
   `/terminal/_next/...`).
2. React hooks (TanStack Query, unchanged) call `/api/terminal/...` on the
   same origin.
3. FastAPI routes call `poseidon.terminal.yahoo`, which serves from TTL cache
   or fetches Yahoo with `httpx.AsyncClient` (one shared client, sane
   timeouts, browser-like User-Agent).
4. Responses are normalized to `lib/types.ts` shapes and returned with
   `Cache-Control: public, s-maxage=<ttl>, stale-while-revalidate=<4×ttl>`
   mirroring the standalone `lib/http.ts` (news: 30 s, matching the v0.2.0
   manual-refresh fix).

## Error handling

- Upstream/Yahoo failure → `{"error": msg}` + 502 (message safe/short; no
  stack traces). Invalid input (unknown range, empty symbols, bad symbol
  after sanitization) → 400 with the same envelope.
- No fabricated data, consistent with house rules: on failure the endpoint
  errors and the React panel shows its existing error state; partial batch
  quote failures degrade per-symbol exactly like `lib/yahoo.ts` (multi-quote
  falls back to per-symbol fetches, missing symbols render `—`).
- Crumb expiry → single transparent re-handshake + retry, then error.

**Auth (addendum, decided at implementation):** Poseidon's optional bearer
token exempts only `/static`. The embed extends the exemption to `/terminal`
and `/api/terminal` — GET-only, keyless public market data; no account,
position, or broker state flows through these paths. On a tokened
non-loopback deployment the terminal is therefore readable without the
token, which matches its data sensitivity (public quotes/news).

## Testing

- **Unit (pytest):** normalizers (percent→fraction, candle drop/sort/dedupe,
  news timestamp coercion), TTL cache behavior, and each route against
  mocked httpx transports (`httpx.MockTransport`, following existing test
  patterns in `tests/`) — fixtures use captured real Yahoo payloads.
- **Contract:** shape assertions keyed to `lib/types.ts` field lists so a
  drifted field name fails loudly.
- **Live smoke:** one opt-in marked test (skipped in the default gate)
  hitting real Yahoo for AAPL quote + chart.
- **UI harness:** `tools/ui_verify.py` gains checks — `/terminal` serves the
  bundle (200 + React root present), `/api/terminal/market` returns the
  overview shape, nav entry present in the dashboard.
- **Gate:** ruff + mypy strict + full pytest suite green, as for any Poseidon
  change.

## Packaging & versioning

- New feature on top of the released v2.5.0 → ships as **2.6.0**
  (`pyproject.toml`, `__init__.py`, PKGBUILD bumped in the release commit,
  per repo convention).
- No `pyproject.toml` dependency changes. Wheel/PKGBUILD pick up the bundle
  via existing packaging config; verify with a local wheel build listing.
- Docs: README feature bullet + `docs/` page (what it is, `/terminal`, bundle
  regeneration workflow, explicitly *not* trading data).

## Risks & mitigations

- **Yahoo crumb flakiness** — the quote/fundamentals handshake is the
  fragile part. Mitigations: cache cookie+crumb, one auto-refresh retry,
  fixtures from real captures, live smoke test kept out of the gate.
- **Contract drift** — Python port silently diverging from `lib/types.ts`.
  Mitigations: contract tests + a spec note in both repos that `types.ts` is
  the single source of truth.
- **Stale committed bundle** — bundle README records source commit; embed
  rebuild is one command.
- **Static-export surprises** (fonts, basePath asset paths) — verified
  empirically during implementation; the embed build is validated by loading
  `/terminal` in the UI harness, not assumed.

## Out of scope (phase 2+)

- Portfolio/positions overlay in the terminal (`PORT` function code).
- Replacing Poseidon's trade-ticket quote or data router with Yahoo data.
- Any change to standalone Trading-Terminal behavior.
