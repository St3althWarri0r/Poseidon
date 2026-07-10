# Embedded Trading Terminal

Poseidon serves the [Trading-Terminal](https://github.com/St3althWarri0r/Trading-Terminal)
market-study cockpit at **`/terminal`** (nav: "Terminal"). Quotes, charts,
fundamentals and news come from keyless public Yahoo endpoints via
`poseidon.terminal` — **study data, not trading data**: nothing here feeds
the data router, risk engine, or order path, and prices may be delayed.

- UI: prebuilt static React bundle at `src/poseidon/api/static/terminal/`
  (generated — see `BUNDLE.md` there; regenerate with
  `EMBED_DEST=<poseidon>/src/poseidon/api/static/terminal npm run build:embed`
  in the Trading-Terminal checkout).
- API: `GET /api/terminal/{quote,chart,search,fundamentals,news,market}` —
  read-only, token-exempt (public market data; see the design spec addendum).
- Contract: response shapes mirror Trading-Terminal's `lib/types.ts`.
