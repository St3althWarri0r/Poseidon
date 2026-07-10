# Trading Terminal Embed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the entire Trading-Terminal market-study app from the Poseidon process — static React bundle at `/terminal`, its six data endpoints reimplemented in Python under `/api/terminal/*` — with Poseidon staying pure Python at runtime.

**Architecture:** The Trading-Terminal repo gains an env-gated static-export build (`output:"export"`, `basePath:"/terminal"`, API base `/api/terminal`). Poseidon gains an isolated `src/poseidon/terminal/` package (keyless Yahoo client on httpx + APIRouter) and serves the committed bundle via StaticFiles. The React client is the contract: responses must match `lib/types.ts` in the Trading-Terminal repo field-for-field.

**Tech Stack:** Python 3.11+ / FastAPI / httpx (already deps; nothing new). Next.js 16 static export (build-time only). pytest with `httpx.MockTransport` / `httpx.ASGITransport` (no network in the gate).

**Spec:** `docs/superpowers/specs/2026-07-09-trading-terminal-embed-design.md` (approved 2026-07-09).

## Global Constraints

- **No new Python dependencies.** `httpx`, `fastapi` are already in `pyproject.toml`.
- **Gate:** `.venv/bin/ruff check src tests tools` + `.venv/bin/mypy src` (strict) + `.venv/bin/pytest` all green after every task. Line length 100. Ruff selects `E,F,W,I,N,UP,B,A,C4,RET,SIM,PTH`.
- **No network in default tests.** Live Yahoo test is env-gated behind `POSEIDON_LIVE_TESTS=1`.
- **Contract truth:** `~/trading-terminal/lib/types.ts`. Optional fields (`sector?`, `industry?`) are OMITTED from JSON when absent, not null.
- **Namespace:** all new endpoints under `/api/terminal/…`; do NOT touch Poseidon's existing `/api/quote/{symbol}`.
- **Percent→fraction quirks (review-verified in the standalone app):** Yahoo quote `dividendYield` and financialData `debtToEquity` arrive as percents and must be stored as fractions (`/100`). `summaryDetail.dividendYield` (fundamentals `perShare.dividendYield`) is ALREADY a fraction — pass through.
- **Poseidon repo:** work on branch `feat/terminal-embed`; conventional-commit messages; do not commit the untracked `CLAUDE.md` files.
- **Trading-Terminal repo (~/trading-terminal):** has NO local git. Edits are verified by builds locally and published at the end via GitHub MCP `push_files` + blob-SHA verification (see Task 12). Do not run `git` there.
- Today's date for docs: 2026-07-09. Poseidon version after this feature: **2.6.0**.
- **Deliberate deviation from CLAUDE.md's "use FakeProvider instead of mocking HTTP":**
  that rule targets the DataRouter/provider layer. The terminal package
  deliberately bypasses DataRouter (spec decision — study data, not trading
  data), and its contract *is* the raw Yahoo request construction, so
  `httpx.MockTransport`/`ASGITransport` (in-process, zero network) is the
  correct test seam here. Do not "fix" these tests to use FakeProvider.

---

### Task 1: Trading-Terminal embed build target

**Files:**
- Modify: `~/trading-terminal/lib/api-client.ts` (6 fetchers + new API_BASE)
- Modify: `~/trading-terminal/next.config.ts`
- Modify: `~/trading-terminal/package.json` (scripts)
- Create: `~/trading-terminal/scripts/build-embed.sh`
- Modify: `~/trading-terminal/README.md` (embed section)

**Interfaces:**
- Produces: `npm run build:embed` → static bundle in `out/` (or synced to `$EMBED_DEST`) whose pages live under basePath `/terminal` and whose data calls go to `/api/terminal/*`. Standalone `npm run dev`/`build` behavior unchanged.

- [ ] **Step 1: Parameterize the API base in `lib/api-client.ts`**

Add after the imports (line ~10), and prefix every fetch path:

```ts
/** Base path for the data API. Standalone serves /api; the Poseidon embed
 *  build points at /api/terminal (inlined at build time via env). */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";
```

Change the six fetchers' URLs (only the template heads change):

```ts
return getJSON<Quote[]>(`${API_BASE}/quote?symbols=${q}`, signal);
// fetchChart:
`${API_BASE}/chart?symbol=${encodeURIComponent(symbol)}&range=${range}`,
// fetchSearch:
return getJSON<SearchResultItem[]>(`${API_BASE}/search?q=${encodeURIComponent(q)}`, signal);
// fetchFundamentals:
return getJSON<Fundamentals>(`${API_BASE}/fundamentals?symbol=${encodeURIComponent(symbol)}`, signal);
// fetchNews:
return getJSON<NewsItem[]>(`${API_BASE}/news${suffix}`, signal);
// fetchMarket:
return getJSON<MarketOverview>(`${API_BASE}/market`, signal);
```

- [ ] **Step 2: Embed mode in `next.config.ts`**

Add at the top of the config object (keep everything else, including
`allowedDevOrigins` and `headers()`):

```ts
const EMBED = process.env.TERMINAL_EMBED === "1";

const nextConfig: NextConfig = {
  // Embed build: static export served by Poseidon under /terminal.
  // (headers() below is inert under `output: "export"` — harmless.)
  ...(EMBED ? { output: "export" as const, basePath: "/terminal" } : {}),
  ...
```

- [ ] **Step 3: Create `scripts/build-embed.sh`**

```bash
#!/usr/bin/env bash
# Static-export build for embedding in Poseidon. Route handlers are
# incompatible with `output: "export"`, so app/api is moved aside for the
# build and always restored (trap).
set -euo pipefail
cd "$(dirname "$0")/.."

trap 'if [ -d .api-excluded ]; then rm -rf app/api; mv .api-excluded app/api; fi' EXIT
mv app/api .api-excluded

TERMINAL_EMBED=1 NEXT_PUBLIC_API_BASE=/api/terminal npx next build

if [ -n "${EMBED_DEST:-}" ]; then
  mkdir -p "$EMBED_DEST"
  rm -rf "${EMBED_DEST:?}"/*
  cp -r out/* "$EMBED_DEST"/
  echo "Bundle synced to $EMBED_DEST"
else
  echo "Bundle in $(pwd)/out — set EMBED_DEST to sync it somewhere"
fi
```

Run: `chmod +x scripts/build-embed.sh`

- [ ] **Step 4: Add the script to `package.json`**

```json
    "build": "next build",
    "build:embed": "scripts/build-embed.sh",
```

- [ ] **Step 5: Verify the embed build**

Run: `cd ~/trading-terminal && npm run build:embed`
Expected: build succeeds; `ls out/` shows `index.html` and `_next/`;
`grep -o '/terminal/_next[^"]*' out/index.html | head -3` shows basePath-prefixed assets;
`grep -c '/api/terminal' out/_next/static/chunks/*.js | grep -v ':0'` shows at least one chunk carrying the embed API base.
`ls app/api` still shows the six route dirs (trap restored it).

- [ ] **Step 6: Verify standalone build is unaffected**

Run: `cd ~/trading-terminal && npm run build`
Expected: passes exactly as before (no basePath, no export).

- [ ] **Step 7: README section**

Add to `~/trading-terminal/README.md` under Deploy:

```markdown
**Embed in Poseidon:** `EMBED_DEST=~/Poseidon/src/poseidon/api/static/terminal npm run build:embed`
builds a static bundle (basePath `/terminal`, data from `/api/terminal/*`) and
syncs it into Poseidon, which serves it natively — see Poseidon's
`docs/terminal.md`.
```

No commit (repo has no local git; published in Task 12).

---

### Task 2: Poseidon branch + `terminal` package + constants

**Files:**
- Create: `src/poseidon/terminal/__init__.py`
- Create: `src/poseidon/terminal/constants.py`
- Test: `tests/unit/test_terminal_constants.py`

**Interfaces:**
- Produces: `MAJOR_INDICES/FUTURES/RATES/COMMODITIES/CRYPTO/CURRENCIES/SECTOR_ETFS: tuple[tuple[str, str], ...]` (symbol, name); `RANGE_CONFIG: dict[str, RangeSpec]` with `RangeSpec(interval: str, days: int | str, label: str)`; `RANGE_KEYS: tuple[str, ...]`.

- [ ] **Step 1: Branch**

```bash
cd ~/Poseidon && git switch -c feat/terminal-embed
```

- [ ] **Step 2: Write the failing test**

`tests/unit/test_terminal_constants.py`:

```python
"""Universe/range constants must mirror trading-terminal's lib/constants.ts."""

from __future__ import annotations

from poseidon.terminal.constants import (
    COMMODITIES,
    CRYPTO,
    CURRENCIES,
    FUTURES,
    MAJOR_INDICES,
    RANGE_CONFIG,
    RANGE_KEYS,
    RATES,
    SECTOR_ETFS,
)


def test_universe_sizes_and_spot_symbols() -> None:
    assert [s for s, _ in MAJOR_INDICES] == ["^GSPC", "^DJI", "^IXIC", "^RUT", "^VIX"]
    assert [s for s, _ in FUTURES] == ["ES=F", "NQ=F", "YM=F"]
    assert [s for s, _ in RATES] == ["^TNX", "^FVX", "^TYX"]
    assert [s for s, _ in COMMODITIES] == ["GC=F", "SI=F", "CL=F", "NG=F"]
    assert [s for s, _ in CRYPTO] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert [s for s, _ in CURRENCIES] == ["EURUSD=X", "GBPUSD=X", "JPY=X", "DX-Y.NYB"]
    assert len(SECTOR_ETFS) == 11 and SECTOR_ETFS[0] == ("XLK", "Technology")


def test_range_config_mirrors_ts() -> None:
    assert RANGE_KEYS == ("1D", "5D", "1M", "6M", "YTD", "1Y", "5Y", "MAX")
    assert RANGE_CONFIG["1D"].interval == "5m" and RANGE_CONFIG["1D"].days == 1
    assert RANGE_CONFIG["5D"].interval == "30m" and RANGE_CONFIG["5D"].days == 5
    assert RANGE_CONFIG["YTD"].days == "ytd"
    assert RANGE_CONFIG["5Y"].interval == "1wk" and RANGE_CONFIG["5Y"].days == 1827
    assert RANGE_CONFIG["MAX"].interval == "1mo" and RANGE_CONFIG["MAX"].days == "max"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_terminal_constants.py -v`
Expected: FAIL — `ModuleNotFoundError: poseidon.terminal`

- [ ] **Step 4: Implement**

`src/poseidon/terminal/__init__.py`:

```python
"""Embedded Trading Terminal: keyless Yahoo market data + static React UI.

Isolated from the trading data router / brokers / risk engine on purpose —
this is study data, not order-path data (docs/terminal.md).
"""
```

`src/poseidon/terminal/constants.py` — mirror of the standalone
`lib/constants.ts` (universes + range map):

```python
"""Static reference data mirrored from trading-terminal lib/constants.ts."""

from __future__ import annotations

from typing import NamedTuple

MAJOR_INDICES: tuple[tuple[str, str], ...] = (
    ("^GSPC", "S&P 500"), ("^DJI", "Dow Jones"), ("^IXIC", "Nasdaq"),
    ("^RUT", "Russell 2000"), ("^VIX", "VIX"),
)
FUTURES: tuple[tuple[str, str], ...] = (
    ("ES=F", "S&P Futures"), ("NQ=F", "Nasdaq Fut"), ("YM=F", "Dow Futures"),
)
RATES: tuple[tuple[str, str], ...] = (
    ("^TNX", "US 10Y"), ("^FVX", "US 5Y"), ("^TYX", "US 30Y"),
)
COMMODITIES: tuple[tuple[str, str], ...] = (
    ("GC=F", "Gold"), ("SI=F", "Silver"), ("CL=F", "Crude Oil"), ("NG=F", "Nat Gas"),
)
CRYPTO: tuple[tuple[str, str], ...] = (
    ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
)
CURRENCIES: tuple[tuple[str, str], ...] = (
    ("EURUSD=X", "EUR/USD"), ("GBPUSD=X", "GBP/USD"), ("JPY=X", "USD/JPY"),
    ("DX-Y.NYB", "US Dollar"),
)
SECTOR_ETFS: tuple[tuple[str, str], ...] = (
    ("XLK", "Technology"), ("XLF", "Financials"), ("XLV", "Health Care"),
    ("XLY", "Cons. Disc."), ("XLP", "Cons. Staples"), ("XLE", "Energy"),
    ("XLI", "Industrials"), ("XLB", "Materials"), ("XLU", "Utilities"),
    ("XLRE", "Real Estate"), ("XLC", "Comm. Svcs"),
)


class RangeSpec(NamedTuple):
    interval: str
    days: int | str  # lookback days, or "ytd" / "max"
    label: str


RANGE_CONFIG: dict[str, RangeSpec] = {
    "1D": RangeSpec("5m", 1, "1 Day"),
    "5D": RangeSpec("30m", 5, "5 Days"),
    "1M": RangeSpec("1d", 31, "1 Month"),
    "6M": RangeSpec("1d", 183, "6 Months"),
    "YTD": RangeSpec("1d", "ytd", "Year to Date"),
    "1Y": RangeSpec("1d", 366, "1 Year"),
    "5Y": RangeSpec("1wk", 1827, "5 Years"),
    "MAX": RangeSpec("1mo", "max", "Max"),
}
RANGE_KEYS: tuple[str, ...] = tuple(RANGE_CONFIG)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_terminal_constants.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Gate + commit**

```bash
.venv/bin/ruff check src tests && .venv/bin/mypy src
git add src/poseidon/terminal tests/unit/test_terminal_constants.py
git commit -m "feat(terminal): package skeleton + universe/range constants"
```

---

### Task 3: Pure helpers + TTL cache (`yahoo.py` part 1)

**Files:**
- Create: `src/poseidon/terminal/yahoo.py`
- Test: `tests/unit/test_terminal_yahoo_helpers.py`

**Interfaces:**
- Produces (consumed by Tasks 4–6): `num(v: object) -> float | None`; `frac_from_pct(v: object) -> float | None`; `text(v: object, fallback: str = "") -> str`; `safe_sym(s: str) -> str`; `simplify_raw(v: object) -> object` (recursively `{"raw": n, "fmt": …}` → `n`); `TTLCache` with `async get_or_fetch(key: str, ttl_s: float, fetch: Callable[[], Awaitable[T]]) -> T`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_terminal_yahoo_helpers.py`:

```python
"""Pure helpers must mirror trading-terminal lib/yahoo.ts semantics."""

from __future__ import annotations

import pytest

from poseidon.terminal.yahoo import TTLCache, frac_from_pct, num, safe_sym, simplify_raw, text


def test_num_accepts_only_finite_numbers() -> None:
    assert num(3.5) == 3.5
    assert num(0) == 0
    assert num(True) is None  # bool is not a market number
    assert num(float("nan")) is None
    assert num(float("inf")) is None
    assert num("3.5") is None
    assert num(None) is None


def test_frac_from_pct() -> None:
    assert frac_from_pct(79.5) == 0.795
    assert frac_from_pct(None) is None


def test_text_and_safe_sym() -> None:
    assert text(None) == ""
    assert text("", "fb") == "fb"
    assert text("x", "fb") == "x"
    assert safe_sym(" aapl ") == "AAPL"
    assert safe_sym("^gspc") == "^GSPC"
    assert safe_sym("eurusd=x") == "EURUSD=X"
    assert safe_sym("BTC-USD;DROP") == "BTC-USDDROP"
    assert len(safe_sym("A" * 40)) == 20


def test_simplify_raw_unwraps_recursively() -> None:
    payload = {
        "marketCap": {"raw": 3.1e12, "fmt": "3.1T"},
        "profile": {"employees": {"raw": 150000, "fmt": "150k"}, "city": "Cupertino"},
        "plain": 7,
    }
    out = simplify_raw(payload)
    assert out == {"marketCap": 3.1e12, "profile": {"employees": 150000, "city": "Cupertino"},
                   "plain": 7}


async def test_ttl_cache_hits_within_ttl_and_refetches_after() -> None:
    cache = TTLCache()
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_fetch("k", 60.0, fetch) == 1
    assert await cache.get_or_fetch("k", 60.0, fetch) == 1  # cached
    assert calls == 1
    cache._store["k"] = (0.0, 1)  # force-expire
    assert await cache.get_or_fetch("k", 60.0, fetch) == 2


async def test_ttl_cache_does_not_cache_failures() -> None:
    cache = TTLCache()

    async def boom() -> int:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await cache.get_or_fetch("k", 60.0, boom)

    async def ok() -> int:
        return 42

    assert await cache.get_or_fetch("k", 60.0, ok) == 42


def test_cache_eviction_over_512() -> None:
    cache = TTLCache()
    for i in range(513):
        cache._store[f"k{i}"] = (0.0, i)  # all expired
    cache.evict_expired()
    assert len(cache._store) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` on `poseidon.terminal.yahoo`

- [ ] **Step 3: Implement in `src/poseidon/terminal/yahoo.py`**

```python
"""Keyless Yahoo Finance client for the embedded terminal.

Faithful Python port of trading-terminal's lib/yahoo.ts (same endpoints
yahoo-finance2 v3 uses, same normalization quirks, same TTLs). Study data
only — never used by the trading data router or risk engine.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

_SYM_RE = re.compile(r"[^A-Z0-9.^=\-]")


def num(v: object) -> float | None:
    """Finite numbers only (bool excluded), else None — mirrors ts num()."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def frac_from_pct(v: object) -> float | None:
    """A Yahoo percent figure (79.5) as a true fraction (0.795)."""
    n = num(v)
    return n / 100 if n is not None else None


def text(v: object, fallback: str = "") -> str:
    return v if isinstance(v, str) and v else fallback


def safe_sym(s: str) -> str:
    """Restrict to characters real Yahoo tickers use (defense in depth)."""
    return _SYM_RE.sub("", s.strip().upper())[:20]


def simplify_raw(v: object) -> object:
    """Collapse Yahoo's ``{"raw": n, "fmt": "…"}`` wrappers recursively.

    yahoo-finance2 does this for its callers; the raw HTTP payloads from
    quoteSummary wrap most numerics this way.
    """
    if isinstance(v, dict):
        if "raw" in v and isinstance(v.get("raw"), (int, float, str)):
            return v["raw"]
        return {k: simplify_raw(x) for k, x in v.items()}
    if isinstance(v, list):
        return [simplify_raw(x) for x in v]
    return v


class TTLCache:
    """Tiny in-memory TTL cache; failures are never cached (mirrors ts)."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    async def get_or_fetch(self, key: str, ttl_s: float, fetch: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        hit = self._store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]  # type: ignore[no-any-return]
        value = await fetch()
        self._store[key] = (now + ttl_s, value)
        if len(self._store) > 512:
            self.evict_expired()
        return value

    def evict_expired(self) -> None:
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._store.items() if exp <= now]:
            del self._store[k]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_helpers.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check src tests && .venv/bin/mypy src
git add src/poseidon/terminal/yahoo.py tests/unit/test_terminal_yahoo_helpers.py
git commit -m "feat(terminal): yahoo helpers + TTL cache"
```

---

### Task 4: Cookie+crumb session (`yahoo.py` part 2)

**Files:**
- Modify: `src/poseidon/terminal/yahoo.py` (append)
- Test: `tests/unit/test_terminal_yahoo_session.py`

**Interfaces:**
- Produces: `class YahooSession(client: httpx.AsyncClient | None = None)` with `async get_json(url: str, params: dict[str, str], *, needs_crumb: bool = False) -> Any` and `async aclose() -> None`. Module-level singleton accessor `session() -> YahooSession`.
- Endpoint facts (from vendored yahoo-finance2 v3 source): cookie bootstrap GET `https://fc.yahoo.com/` (any status; sets cookie), fallback GET `https://finance.yahoo.com/quote/AAPL`; crumb GET `https://query1.finance.yahoo.com/v1/test/getcrumb` (plain-text body, needs cookies + `origin`/`referer` headers). Crumb rides as query param `crumb`. One re-bootstrap retry on 401/403.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_terminal_yahoo_session.py`:

```python
"""YahooSession crumb flow against httpx.MockTransport (no network)."""

from __future__ import annotations

import httpx
import pytest

from poseidon.core.errors import DataError
from poseidon.terminal.yahoo import YahooSession


def make_session(handler: httpx.MockTransport) -> YahooSession:
    return YahooSession(client=httpx.AsyncClient(transport=handler))


async def test_crumb_fetched_once_and_attached() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404, headers={"set-cookie": "A3=abc; Domain=.yahoo.com"})
        if req.url.path == "/v1/test/getcrumb":
            return httpx.Response(200, text="CRUMB123")
        if req.url.path == "/v7/finance/quote":
            assert req.url.params["crumb"] == "CRUMB123"
            return httpx.Response(200, json={"quoteResponse": {"result": []}})
        raise AssertionError(f"unexpected {req.url}")

    s = make_session(httpx.MockTransport(handler))
    out = await s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                           {"symbols": "AAPL"}, needs_crumb=True)
    assert out == {"quoteResponse": {"result": []}}
    # Second call reuses the cached crumb (no extra bootstrap requests).
    n = len(seen)
    await s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                     {"symbols": "MSFT"}, needs_crumb=True)
    assert len(seen) == n + 1


async def test_no_crumb_endpoints_skip_bootstrap() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "crumb" not in req.url.params
        assert req.url.path.startswith("/v8/finance/chart/")
        return httpx.Response(200, json={"chart": {"result": [{}]}})

    s = make_session(httpx.MockTransport(handler))
    out = await s.get_json("https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
                           {"interval": "1d"})
    assert out == {"chart": {"result": [{}]}}


async def test_forbidden_triggers_one_rebootstrap_then_succeeds() -> None:
    crumbs = iter(["OLD", "NEW"])
    quote_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if req.url.path == "/v1/test/getcrumb":
            return httpx.Response(200, text=next(crumbs))
        if req.url.path == "/v7/finance/quote":
            quote_calls.append(req.url.params["crumb"])
            if req.url.params["crumb"] == "OLD":
                return httpx.Response(401)
            return httpx.Response(200, json={"quoteResponse": {"result": []}})
        raise AssertionError(str(req.url))

    s = make_session(httpx.MockTransport(handler))
    await s.get_json("https://query2.finance.yahoo.com/v7/finance/quote",
                     {"symbols": "AAPL"}, needs_crumb=True)
    assert quote_calls == ["OLD", "NEW"]


async def test_upstream_error_raises_dataerror() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    s = make_session(httpx.MockTransport(handler))
    with pytest.raises(DataError):
        await s.get_json("https://query2.finance.yahoo.com/v8/finance/chart/AAPL", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_session.py -v`
Expected: FAIL — `ImportError: cannot import name 'YahooSession'`

- [ ] **Step 3: Implement — append to `src/poseidon/terminal/yahoo.py`**

Add imports at top: `import asyncio`, `import httpx`, `import structlog`,
`from ..core.errors import DataError`.

```python
log = structlog.get_logger(__name__)

_UA = "Mozilla/5.0 (compatible; poseidon-terminal/1.0)"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_COOKIE_URLS = ("https://fc.yahoo.com/", "https://finance.yahoo.com/quote/AAPL")


class YahooSession:
    """Shared httpx client with Yahoo's cookie+crumb handshake."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=10.0, follow_redirects=True, headers={"User-Agent": _UA})
        self._crumb: str | None = None
        self._lock = asyncio.Lock()

    async def _bootstrap(self) -> None:
        async with self._lock:
            for cookie_url in _COOKIE_URLS:
                try:
                    await self._client.get(cookie_url)  # any status; sets cookies
                    r = await self._client.get(_CRUMB_URL, headers={
                        "origin": "https://finance.yahoo.com",
                        "referer": "https://finance.yahoo.com/quote/AAPL",
                        "accept": "*/*",
                    })
                    if r.status_code == 200 and r.text and "<" not in r.text:
                        self._crumb = r.text.strip()
                        return
                except httpx.HTTPError as exc:
                    log.debug("terminal.crumb_bootstrap_failed", url=cookie_url, err=str(exc))
        raise DataError("Yahoo crumb handshake failed")

    async def get_json(self, url: str, params: dict[str, str], *,
                       needs_crumb: bool = False) -> Any:
        for attempt in (1, 2):
            q = dict(params)
            if needs_crumb:
                if self._crumb is None:
                    await self._bootstrap()
                q["crumb"] = self._crumb or ""
            try:
                r = await self._client.get(url, params=q)
            except httpx.HTTPError as exc:
                raise DataError(f"Yahoo request failed: {exc}") from exc
            if r.status_code in (401, 403) and needs_crumb and attempt == 1:
                self._crumb = None  # stale crumb — re-handshake once
                continue
            if r.status_code != 200:
                raise DataError(f"Yahoo returned HTTP {r.status_code}")
            return r.json()
        raise DataError("Yahoo auth retry exhausted")  # pragma: no cover

    async def aclose(self) -> None:
        await self._client.aclose()


_session: YahooSession | None = None


def session() -> YahooSession:
    global _session  # noqa: PLW0603 — module singleton, single event loop
    if _session is None:
        _session = YahooSession()
    return _session
```

Note: if ruff flags the `global` (PLW isn't in the select list, so it won't),
drop the noqa. `DataError` exists in `poseidon.core.errors` (used by data
providers) — reuse it, don't invent a new error type.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_session.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check src tests && .venv/bin/mypy src
git add -u && git add tests/unit/test_terminal_yahoo_session.py
git commit -m "feat(terminal): Yahoo cookie+crumb session with single-retry refresh"
```

---

### Task 5: Normalizers (`yahoo.py` part 3)

**Files:**
- Modify: `src/poseidon/terminal/yahoo.py` (append)
- Test: `tests/unit/test_terminal_yahoo_normalize.py`

**Interfaces:**
- Produces: `normalize_quote(q: Any) -> dict[str, Any]` (full Quote shape, 27 keys); `normalize_candles(result: Any) -> list[dict[str, Any]]` (chart.result[0] → sorted, deduped candles); `normalize_news(items: Any) -> list[dict[str, Any]]`; `normalize_search(quotes: Any) -> list[dict[str, Any]]`; `normalize_fundamentals(sym: str, qs: Any) -> dict[str, Any]` (input is the SIMPLIFIED quoteSummary.result[0]).
- Contract: field names exactly as in `~/trading-terminal/lib/types.ts` (Quote / Candle / ChartResponse / SearchResultItem / NewsItem / Fundamentals).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_terminal_yahoo_normalize.py`:

```python
"""Normalizers must reproduce lib/yahoo.ts field mapping and quirks."""

from __future__ import annotations

from poseidon.terminal.yahoo import (
    normalize_candles,
    normalize_fundamentals,
    normalize_news,
    normalize_quote,
    normalize_search,
)

QUOTE_KEYS = {
    "symbol", "name", "quoteType", "currency", "exchange", "marketState",
    "price", "change", "changePercent", "previousClose", "open", "dayHigh", "dayLow",
    "volume", "avgVolume", "marketCap", "trailingPE", "forwardPE", "eps",
    "dividendYield", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage",
    "twoHundredDayAverage", "sharesOutstanding", "postMarketPrice", "postMarketChange",
    "postMarketChangePercent", "preMarketPrice", "preMarketChange",
    "preMarketChangePercent",
}


def test_quote_shape_and_percent_quirk() -> None:
    q = normalize_quote({
        "symbol": "AAPL", "longName": "Apple Inc.", "quoteType": "EQUITY",
        "currency": "USD", "fullExchangeName": "NasdaqGS", "marketState": "REGULAR",
        "regularMarketPrice": 314.66, "regularMarketChange": 1.25,
        "regularMarketChangePercent": 0.4, "regularMarketVolume": 1000,
        "averageDailyVolume3Month": 2000, "dividendYield": 0.44, "trailingPE": 38.05,
    })
    assert set(q) == QUOTE_KEYS
    assert q["name"] == "Apple Inc."
    assert q["dividendYield"] == 0.0044  # percent -> fraction
    assert q["avgVolume"] == 2000
    assert q["eps"] is None  # absent -> null, key still present


def test_quote_name_fallback_chain() -> None:
    assert normalize_quote({"symbol": "X"})["name"] == "X"
    assert normalize_quote({"symbol": "X", "shortName": "Short"})["name"] == "Short"


def test_candles_drop_gaps_sort_and_dedupe_last_wins() -> None:
    result = {
        "timestamp": [30, 10, 10, 20],
        "indicators": {"quote": [{
            "open":  [3.0, 1.0, 1.5, None],
            "high":  [3.5, 1.2, 1.6, 2.2],
            "low":   [2.9, 0.9, 1.4, 1.9],
            "close": [3.2, 1.1, 1.5, 2.1],
            "volume": [None, 100, 150, 200],
        }]},
    }
    candles = normalize_candles(result)
    # ts=20 dropped (open null); ts=10 deduped last-wins (open 1.5); sorted asc.
    assert [c["time"] for c in candles] == [10, 30]
    assert candles[0]["open"] == 1.5
    assert candles[1]["volume"] == 0  # null volume -> 0


def test_news_normalization() -> None:
    items = normalize_news([
        {"uuid": "u1", "title": "T", "publisher": "P", "link": "https://x",
         "providerPublishTime": 1_700_000_000,
         "thumbnail": {"resolutions": [{"url": "https://img"}]},
         "relatedTickers": ["AAPL", 5]},
        {"title": "no link -> dropped"},
    ])
    assert len(items) == 1
    n = items[0]
    assert n["id"] == "u1" and n["publishedAt"] == 1_700_000_000_000
    assert n["thumbnail"] == "https://img" and n["tickers"] == ["AAPL", "5"]


def test_news_iso_publish_time() -> None:
    (n,) = normalize_news([{"title": "T", "link": "https://x",
                            "providerPublishTime": "2026-07-09T12:00:00Z"}])
    assert n["publishedAt"] == 1_783_598_400_000
    assert n["publisher"] == "—"


def test_search_optional_fields_omitted() -> None:
    out = normalize_search([
        {"symbol": "AAPL", "longname": "Apple", "exchDisp": "NASDAQ",
         "quoteType": "EQUITY", "sector": "Tech"},
        {"symbol": "ZZZ", "shortname": "Zed"},
        {"noSymbol": True},
    ])
    assert len(out) == 2
    assert out[0]["sector"] == "Tech" and "industry" not in out[0]
    assert "sector" not in out[1] and out[1]["type"] == "EQUITY"


def test_fundamentals_mapping_and_debt_quirk() -> None:
    f = normalize_fundamentals("AAPL", {
        "assetProfile": {"sector": "Technology", "fullTimeEmployees": 150000},
        "summaryDetail": {"marketCap": 3.1e12, "dividendYield": 0.0044,
                          "trailingPE": 38.05},
        "financialData": {"totalRevenue": 4.0e11, "debtToEquity": 79.55,
                          "grossMargins": 0.45},
        "defaultKeyStatistics": {"trailingEps": 7.1},
        "price": {"longName": "Apple Inc."},
    })
    assert set(f) == {"symbol", "profile", "valuation", "financials", "perShare", "targets"}
    assert f["profile"]["name"] == "Apple Inc."
    assert f["profile"]["employees"] == 150000
    assert f["valuation"]["marketCap"] == 3.1e12
    assert f["financials"]["debtToEquity"] == 0.7955  # percent -> ratio
    assert f["perShare"]["dividendYield"] == 0.0044   # already a fraction
    assert f["perShare"]["eps"] == 7.1
    assert f["targets"]["recommendationKey"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_normalize.py -v`
Expected: FAIL — ImportError on normalize_* names

- [ ] **Step 3: Implement — append to `src/poseidon/terminal/yahoo.py`**

Add `from datetime import datetime` to imports.

```python
def normalize_quote(q: Any) -> dict[str, Any]:
    g = q or {}
    return {
        "symbol": text(g.get("symbol")),
        "name": text(g.get("longName")) or text(g.get("shortName")) or text(g.get("symbol")),
        "quoteType": text(g.get("quoteType"), "EQUITY"),
        "currency": text(g.get("currency"), "USD"),
        "exchange": text(g.get("fullExchangeName")) or text(g.get("exchange")),
        "marketState": text(g.get("marketState"), "CLOSED"),
        "price": num(g.get("regularMarketPrice")),
        "change": num(g.get("regularMarketChange")),
        "changePercent": num(g.get("regularMarketChangePercent")),
        "previousClose": num(g.get("regularMarketPreviousClose")),
        "open": num(g.get("regularMarketOpen")),
        "dayHigh": num(g.get("regularMarketDayHigh")),
        "dayLow": num(g.get("regularMarketDayLow")),
        "volume": num(g.get("regularMarketVolume")),
        "avgVolume": num(g.get("averageDailyVolume3Month"))
        if num(g.get("averageDailyVolume3Month")) is not None
        else num(g.get("averageDailyVolume10Day")),
        "marketCap": num(g.get("marketCap")),
        "trailingPE": num(g.get("trailingPE")),
        "forwardPE": num(g.get("forwardPE")),
        "eps": num(g.get("epsTrailingTwelveMonths")),
        # Yahoo quote dividendYield is a percent (0.44 = 0.44%); store fraction.
        "dividendYield": frac_from_pct(g.get("dividendYield")),
        "beta": num(g.get("beta")),
        "fiftyTwoWeekHigh": num(g.get("fiftyTwoWeekHigh")),
        "fiftyTwoWeekLow": num(g.get("fiftyTwoWeekLow")),
        "fiftyDayAverage": num(g.get("fiftyDayAverage")),
        "twoHundredDayAverage": num(g.get("twoHundredDayAverage")),
        "sharesOutstanding": num(g.get("sharesOutstanding")),
        "postMarketPrice": num(g.get("postMarketPrice")),
        "postMarketChange": num(g.get("postMarketChange")),
        "postMarketChangePercent": num(g.get("postMarketChangePercent")),
        "preMarketPrice": num(g.get("preMarketPrice")),
        "preMarketChange": num(g.get("preMarketChange")),
        "preMarketChangePercent": num(g.get("preMarketChangePercent")),
    }


def normalize_candles(result: Any) -> list[dict[str, Any]]:
    r = result or {}
    ts: list[Any] = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    vols = quote.get("volume") or []

    candles: list[dict[str, Any]] = []
    for i, t in enumerate(ts):
        o = num(opens[i] if i < len(opens) else None)
        h = num(highs[i] if i < len(highs) else None)
        lo = num(lows[i] if i < len(lows) else None)
        c = num(closes[i] if i < len(closes) else None)
        tt = num(t)
        if o is None or h is None or lo is None or c is None or tt is None:
            continue  # Yahoo pads gaps with nulls — they'd break the chart
        v = num(vols[i] if i < len(vols) else None)
        candles.append({"time": int(tt), "open": o, "high": h, "low": lo,
                        "close": c, "volume": v if v is not None else 0})

    candles.sort(key=lambda c: c["time"])
    deduped: list[dict[str, Any]] = []
    for c in candles:  # strictly-ascending, last-wins (lightweight-charts rule)
        if deduped and deduped[-1]["time"] == c["time"]:
            deduped[-1] = c
        else:
            deduped.append(c)
    return deduped


def _publish_ms(t: Any) -> int | None:
    n = num(t)
    if n is not None:
        return int(n * 1000)
    if isinstance(t, str):
        try:
            return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def normalize_news(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in items or []:
        if not (n and n.get("title") and n.get("link")):
            continue
        thumbs = (n.get("thumbnail") or {}).get("resolutions")
        thumb = text(thumbs[0].get("url")) or None if isinstance(thumbs, list) and thumbs else None
        tickers = n.get("relatedTickers")
        out.append({
            "id": text(n.get("uuid")) or text(n.get("link")),
            "title": text(n.get("title")),
            "publisher": text(n.get("publisher"), "—"),
            "link": text(n.get("link")),
            "publishedAt": _publish_ms(n.get("providerPublishTime")),
            "thumbnail": thumb,
            "tickers": [str(t) for t in tickers] if isinstance(tickers, list) else [],
        })
    return out


def normalize_search(quotes: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in quotes or []:
        if not (r and r.get("symbol")):
            continue
        item: dict[str, Any] = {
            "symbol": text(r.get("symbol")),
            "name": text(r.get("longname")) or text(r.get("shortname")) or text(r.get("symbol")),
            "exchange": text(r.get("exchDisp")) or text(r.get("exchange")),
            "type": text(r.get("quoteType"), "EQUITY"),
        }
        if r.get("sector"):
            item["sector"] = text(r.get("sector"))
        if r.get("industry"):
            item["industry"] = text(r.get("industry"))
        out.append(item)
    return out


def normalize_fundamentals(sym: str, qs: Any) -> dict[str, Any]:
    d = qs or {}
    ap, sd = d.get("assetProfile") or {}, d.get("summaryDetail") or {}
    fd, ks = d.get("financialData") or {}, d.get("defaultKeyStatistics") or {}
    pr = d.get("price") or {}

    def first(*vals: float | None) -> float | None:
        for v in vals:
            if v is not None:
                return v
        return None

    return {
        "symbol": sym,
        "profile": {
            "name": text(pr.get("longName")) or text(pr.get("shortName")) or sym,
            "sector": text(ap.get("sector")) or None,
            "industry": text(ap.get("industry")) or None,
            "employees": num(ap.get("fullTimeEmployees")),
            "country": text(ap.get("country")) or None,
            "city": text(ap.get("city")) or None,
            "website": text(ap.get("website")) or None,
            "summary": text(ap.get("longBusinessSummary")) or None,
        },
        "valuation": {
            "marketCap": first(num(sd.get("marketCap")), num(pr.get("marketCap"))),
            "enterpriseValue": num(ks.get("enterpriseValue")),
            "trailingPE": num(sd.get("trailingPE")),
            "forwardPE": first(num(sd.get("forwardPE")), num(ks.get("forwardPE"))),
            "pegRatio": num(ks.get("pegRatio")),
            "priceToBook": num(ks.get("priceToBook")),
            "priceToSales": num(sd.get("priceToSalesTrailing12Months")),
            "enterpriseToEbitda": num(ks.get("enterpriseToEbitda")),
            "beta": first(num(sd.get("beta")), num(ks.get("beta"))),
        },
        "financials": {
            "revenue": num(fd.get("totalRevenue")),
            "revenueGrowth": num(fd.get("revenueGrowth")),
            "grossMargins": num(fd.get("grossMargins")),
            "operatingMargins": num(fd.get("operatingMargins")),
            "profitMargins": first(num(fd.get("profitMargins")), num(ks.get("profitMargins"))),
            "ebitda": num(fd.get("ebitda")),
            "freeCashflow": num(fd.get("freeCashflow")),
            "operatingCashflow": num(fd.get("operatingCashflow")),
            "totalCash": num(fd.get("totalCash")),
            "totalDebt": num(fd.get("totalDebt")),
            # Yahoo debtToEquity is a percent (79.55 = 0.7955x); store the ratio.
            "debtToEquity": frac_from_pct(fd.get("debtToEquity")),
            "returnOnEquity": num(fd.get("returnOnEquity")),
            "returnOnAssets": num(fd.get("returnOnAssets")),
            "currentRatio": num(fd.get("currentRatio")),
        },
        "perShare": {
            "eps": num(ks.get("trailingEps")),
            "forwardEps": num(ks.get("forwardEps")),
            "bookValue": num(ks.get("bookValue")),
            "dividendRate": num(sd.get("dividendRate")),
            "dividendYield": num(sd.get("dividendYield")),  # already a fraction here
            "payoutRatio": num(sd.get("payoutRatio")),
        },
        "targets": {
            "currentPrice": num(fd.get("currentPrice")),
            "targetMean": num(fd.get("targetMeanPrice")),
            "targetHigh": num(fd.get("targetHighPrice")),
            "targetLow": num(fd.get("targetLowPrice")),
            "recommendationKey": text(fd.get("recommendationKey")) or None,
            "numberOfAnalysts": num(fd.get("numberOfAnalystOpinions")),
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_normalize.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check src tests && .venv/bin/mypy src
git add -u && git add tests/unit/test_terminal_yahoo_normalize.py
git commit -m "feat(terminal): Yahoo response normalizers (types.ts contract)"
```

---

### Task 6: Public data API (`yahoo.py` part 4)

**Files:**
- Modify: `src/poseidon/terminal/yahoo.py` (append)
- Test: `tests/unit/test_terminal_yahoo_api.py`

**Interfaces:**
- Consumes: Task 3–5 helpers, `constants.py` universes.
- Produces (consumed by routes, Task 7): `async get_quotes(symbols: list[str]) -> list[dict[str, Any]]`; `async get_chart(symbol: str, range_key: str) -> dict[str, Any]`; `async search_symbols(q: str) -> list[dict[str, Any]]`; `async get_news(symbol: str | None) -> list[dict[str, Any]]`; `async get_fundamentals(symbol: str) -> dict[str, Any]`; `async get_market_overview() -> dict[str, Any]`. All go through the module-level `_cache = TTLCache()` and `session()`.
- Yahoo request shapes (from vendored yahoo-finance2 v3): quote `GET query2.finance.yahoo.com/v7/finance/quote?symbols=A,B&crumb=…`, results at `quoteResponse.result[]`, drop `quoteType == "NONE"`; chart `GET query2…/v8/finance/chart/{sym}?period1=<epoch-s>&period2=<epoch-s>&interval=…&includePrePost=true&events=div|split|earn`, result at `chart.result[0]`; search `GET query2…/v1/finance/search?q=…&quotesCount=…&newsCount=…&lang=en-US&region=US`, top-level `quotes[]`/`news[]`; quoteSummary `GET query2…/v10/finance/quoteSummary/{sym}?modules=a,b&formatted=false&crumb=…`, result at `quoteSummary.result[0]` (then `simplify_raw`).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_terminal_yahoo_api.py`:

```python
"""Public data functions: request shapes, caching, batch fallback (no network)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import poseidon.terminal.yahoo as ty
from poseidon.core.errors import DataError


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ty, "_cache", ty.TTLCache())
    monkeypatch.setattr(ty, "_session", None)


def install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    s = ty.YahooSession(client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    monkeypatch.setattr(ty, "_session", s)
    return seen


def bootstrap_ok(req: httpx.Request) -> httpx.Response | None:
    if req.url.host == "fc.yahoo.com":
        return httpx.Response(404)
    if req.url.path == "/v1/test/getcrumb":
        return httpx.Response(200, text="C")
    return None


async def test_get_quotes_batches_sorts_dedupes_and_caches(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        assert req.url.params["symbols"] == "AAPL,MSFT"  # deduped + sorted
        return httpx.Response(200, json={"quoteResponse": {"result": [
            {"symbol": "AAPL", "regularMarketPrice": 1.0},
            {"symbol": "MSFT", "regularMarketPrice": 2.0},
            {"symbol": "DEAD", "quoteType": "NONE"},
        ]}})

    seen = install(monkeypatch, handler)
    out = await ty.get_quotes(["msft", "AAPL", "aapl"])
    assert [q["symbol"] for q in out] == ["AAPL", "MSFT"]  # NONE filtered
    n = len(seen)
    await ty.get_quotes(["AAPL", "MSFT"])  # same key -> cache, no new request
    assert len(seen) == n


async def test_get_quotes_falls_back_per_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        syms = req.url.params["symbols"]
        if "," in syms:
            return httpx.Response(500)
        if syms == "BAD":
            return httpx.Response(500)
        return httpx.Response(200, json={"quoteResponse": {"result": [
            {"symbol": syms, "regularMarketPrice": 9.9}]}})

    install(monkeypatch, handler)
    out = await ty.get_quotes(["GOOD", "BAD"])
    assert [q["symbol"] for q in out] == ["GOOD"]  # one failure can't blank the panel


async def test_get_chart_params_and_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v8/finance/chart/AAPL"
        assert req.url.params["interval"] == "1d"
        assert int(req.url.params["period1"]) < int(req.url.params["period2"])
        assert "crumb" not in req.url.params
        return httpx.Response(200, json={"chart": {"result": [{
            "meta": {"symbol": "AAPL", "currency": "USD",
                     "fullExchangeName": "NasdaqGS",
                     "regularMarketPrice": 314.66, "previousClose": 313.39},
            "timestamp": [100], "indicators": {"quote": [{
                "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
                "volume": [10]}]},
        }]}})

    install(monkeypatch, handler)
    out = await ty.get_chart("aapl", "1M")
    assert out["symbol"] == "AAPL" and out["exchangeName"] == "NasdaqGS"
    assert out["candles"] == [{"time": 100, "open": 1.0, "high": 2.0, "low": 0.5,
                               "close": 1.5, "volume": 10}]


async def test_get_chart_rejects_bad_range(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda req: httpx.Response(500))
    with pytest.raises(DataError, match="range"):
        await ty.get_chart("AAPL", "2W")


async def test_search_and_news_share_search_endpoint(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/finance/search"
        if req.url.params["newsCount"] == "0":
            return httpx.Response(200, json={"quotes": [{"symbol": "AAPL"}], "news": []})
        assert req.url.params["newsCount"] == "12"
        return httpx.Response(200, json={"quotes": [], "news": [
            {"title": "T", "link": "https://x", "providerPublishTime": 1}]})

    install(monkeypatch, handler)
    assert (await ty.search_symbols("apple"))[0]["symbol"] == "AAPL"
    assert (await ty.get_news("AAPL"))[0]["publishedAt"] == 1000
    assert (await ty.get_news(None))[0]["title"] == "T"  # market-wide query


async def test_fundamentals_simplifies_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        assert req.url.path == "/v10/finance/quoteSummary/AAPL"
        assert req.url.params["modules"] == (
            "assetProfile,summaryDetail,financialData,defaultKeyStatistics,price")
        return httpx.Response(200, json={"quoteSummary": {"result": [{
            "summaryDetail": {"marketCap": {"raw": 3.1e12, "fmt": "3.1T"}},
            "financialData": {"debtToEquity": {"raw": 79.55, "fmt": "79.55%"}},
            "price": {"longName": "Apple Inc."},
        }]}})

    install(monkeypatch, handler)
    f = await ty.get_fundamentals("AAPL")
    assert f["valuation"]["marketCap"] == 3.1e12
    assert f["financials"]["debtToEquity"] == 0.7955


async def test_market_overview_shape_and_sector_sort(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (b := bootstrap_ok(req)) is not None:
            return b
        syms = req.url.params["symbols"].split(",")
        return httpx.Response(200, json={"quoteResponse": {"result": [
            {"symbol": s, "regularMarketChangePercent":
                (None if s == "XLC" else float(i))}
            for i, s in enumerate(syms)]}})

    install(monkeypatch, handler)
    mkt = await ty.get_market_overview()
    assert set(mkt) == {"indices", "futures", "rates", "commodities", "crypto",
                        "currencies", "sectors"}
    assert [q["symbol"] for q in mkt["indices"]] == ["^GSPC", "^DJI", "^IXIC", "^RUT", "^VIX"]
    sectors = mkt["sectors"]
    assert len(sectors) == 11 and sectors[-1]["symbol"] == "XLC"  # null sorts last
    changes = [s["changePercent"] for s in sectors if s["changePercent"] is not None]
    assert changes == sorted(changes, reverse=True)
    assert sectors[0]["name"]  # names come from SECTOR_ETFS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_api.py -v`
Expected: FAIL — AttributeError/ImportError on `get_quotes` etc.

- [ ] **Step 3: Implement — append to `src/poseidon/terminal/yahoo.py`**

Add to imports: `from datetime import timedelta`, and
`from .constants import (COMMODITIES, CRYPTO, CURRENCIES, FUTURES, MAJOR_INDICES, RANGE_CONFIG, RATES, SECTOR_ETFS)`.

```python
_cache = TTLCache()

_Q2 = "https://query2.finance.yahoo.com"
_MODULES = "assetProfile,summaryDetail,financialData,defaultKeyStatistics,price"


async def _quotes_uncached(clean: list[str]) -> list[dict[str, Any]]:
    async def one_call(symbols: str) -> list[Any]:
        raw = await session().get_json(f"{_Q2}/v7/finance/quote",
                                       {"symbols": symbols}, needs_crumb=True)
        result = (raw or {}).get("quoteResponse", {}).get("result") or []
        return [q for q in result if q and q.get("quoteType") != "NONE"]

    try:
        rows = await one_call(",".join(clean))
    except DataError:
        # Graceful degradation: one bad symbol can't blank an entire panel.
        results = await asyncio.gather(*(one_call(s) for s in clean),
                                       return_exceptions=True)
        rows = [r[0] for r in results if isinstance(r, list) and r]
    return [normalize_quote(q) for q in rows]


async def get_quotes(symbols: list[str]) -> list[dict[str, Any]]:
    clean = sorted({s for s in (safe_sym(x) for x in symbols) if s})
    if not clean:
        return []
    return await _cache.get_or_fetch(
        f"quotes:{','.join(clean)}", 10.0, lambda: _quotes_uncached(clean))


def _period1(range_key: str) -> int:
    spec = RANGE_CONFIG[range_key]
    now = datetime.now().astimezone()
    if spec.days == "ytd":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif spec.days == "max":
        return 86_400  # 1970-01-02, mirroring lib/yahoo.ts
    else:
        start = now - timedelta(days=int(spec.days))
    return int(start.timestamp())


async def get_chart(symbol: str, range_key: str) -> dict[str, Any]:
    if range_key not in RANGE_CONFIG:
        raise DataError(f"Invalid range: {range_key}")
    sym = safe_sym(symbol)
    ttl = 30.0 if range_key in ("1D", "5D") else 120.0

    async def fetch() -> dict[str, Any]:
        raw = await session().get_json(f"{_Q2}/v8/finance/chart/{sym}", {
            "period1": str(_period1(range_key)),
            "period2": str(int(datetime.now().timestamp())),
            "interval": RANGE_CONFIG[range_key].interval,
            "includePrePost": "true",
            "events": "div|split|earn",
        })
        result = ((raw or {}).get("chart", {}).get("result") or [{}])[0] or {}
        meta = result.get("meta") or {}
        return {
            "symbol": text(meta.get("symbol"), sym),
            "currency": text(meta.get("currency"), "USD"),
            "exchangeName": text(meta.get("fullExchangeName")) or text(meta.get("exchangeName")),
            "regularMarketPrice": num(meta.get("regularMarketPrice")),
            "previousClose": num(meta.get("previousClose"))
            if num(meta.get("previousClose")) is not None
            else num(meta.get("chartPreviousClose")),
            "candles": normalize_candles(result),
        }

    return await _cache.get_or_fetch(f"chart:{sym}:{range_key}", ttl, fetch)


async def search_symbols(q: str) -> list[dict[str, Any]]:
    query = q.strip()
    if not query:
        return []

    async def fetch() -> list[dict[str, Any]]:
        raw = await session().get_json(f"{_Q2}/v1/finance/search", {
            "q": query, "quotesCount": "10", "newsCount": "0",
            "lang": "en-US", "region": "US",
        })
        return normalize_search((raw or {}).get("quotes"))

    return await _cache.get_or_fetch(f"search:{query.lower()}", 60.0, fetch)


async def get_news(symbol: str | None) -> list[dict[str, Any]]:
    query = safe_sym(symbol) if symbol and symbol.strip() else "stock market"

    async def fetch() -> list[dict[str, Any]]:
        raw = await session().get_json(f"{_Q2}/v1/finance/search", {
            "q": query, "quotesCount": "0", "newsCount": "12",
            "lang": "en-US", "region": "US",
        })
        return normalize_news((raw or {}).get("news"))

    # Short TTL so the panel's manual refresh pulls genuinely fresh headlines.
    return await _cache.get_or_fetch(f"news:{query.lower()}", 30.0, fetch)


async def get_fundamentals(symbol: str) -> dict[str, Any]:
    sym = safe_sym(symbol)

    async def fetch() -> dict[str, Any]:
        raw = await session().get_json(f"{_Q2}/v10/finance/quoteSummary/{sym}", {
            "modules": _MODULES, "formatted": "false",
        }, needs_crumb=True)
        result = ((raw or {}).get("quoteSummary", {}).get("result") or [{}])[0]
        return normalize_fundamentals(sym, simplify_raw(result))

    return await _cache.get_or_fetch(f"fundamentals:{sym}", 6 * 3600.0, fetch)


async def _quotes_for(universe: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
    try:
        quotes = await get_quotes([s for s, _ in universe])
    except DataError:
        return []
    by_sym = {q["symbol"]: q for q in quotes}
    return [by_sym[s] for s, _ in universe if s in by_sym]  # configured display order


async def get_market_overview() -> dict[str, Any]:
    async def fetch() -> dict[str, Any]:
        indices, futures, rates, commodities, crypto, currencies, sector_quotes = (
            await asyncio.gather(
                _quotes_for(MAJOR_INDICES), _quotes_for(FUTURES), _quotes_for(RATES),
                _quotes_for(COMMODITIES), _quotes_for(CRYPTO), _quotes_for(CURRENCIES),
                _quotes_for(SECTOR_ETFS),
            ))
        names = dict(SECTOR_ETFS)
        sectors = sorted(
            ({"symbol": q["symbol"], "name": names.get(q["symbol"], q["symbol"]),
              "changePercent": q["changePercent"]} for q in sector_quotes),
            key=lambda s: s["changePercent"]
            if s["changePercent"] is not None else float("-inf"),
            reverse=True,
        )
        return {"indices": indices, "futures": futures, "rates": rates,
                "commodities": commodities, "crypto": crypto,
                "currencies": currencies, "sectors": sectors}

    return await _cache.get_or_fetch("market-overview", 15.0, fetch)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_terminal_yahoo_api.py -v`
Expected: PASS (7 tests). Also rerun the whole module's tests:
`.venv/bin/pytest tests/unit/test_terminal_yahoo_helpers.py tests/unit/test_terminal_yahoo_session.py tests/unit/test_terminal_yahoo_normalize.py -q` → all pass.

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check src tests && .venv/bin/mypy src
git add -u && git add tests/unit/test_terminal_yahoo_api.py
git commit -m "feat(terminal): public data API with TTLs and batch fallback"
```

---

### Task 7: FastAPI routes (`routes.py`)

**Files:**
- Create: `src/poseidon/terminal/routes.py`
- Test: `tests/unit/test_terminal_routes.py`

**Interfaces:**
- Consumes: Task 6 functions **via module attribute** (`from . import yahoo` then `yahoo.get_quotes(…)`) so tests and ui_verify can monkeypatch `poseidon.terminal.yahoo.<fn>`.
- Produces: `router: APIRouter` with prefix `/api/terminal`, six GET endpoints matching the standalone `app/api/*/route.ts` param shapes, `Cache-Control: public, s-maxage=<n>, stale-while-revalidate=<4n>` on success, `{"error": msg}` envelope on failure.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_terminal_routes.py`:

```python
"""Route contract: params, envelopes, cache headers (ASGI, no network)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import poseidon.terminal.yahoo as ty
from poseidon.core.errors import DataError
from poseidon.terminal.routes import router


def client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def test_quote_param_validation_and_header(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(symbols: list[str]) -> list[dict[str, Any]]:
        return [{"symbol": s} for s in symbols]

    monkeypatch.setattr(ty, "get_quotes", fake)
    async with client() as c:
        r = await c.get("/api/terminal/quote?symbols=AAPL,MSFT")
        assert r.status_code == 200 and [q["symbol"] for q in r.json()] == ["AAPL", "MSFT"]
        assert r.headers["cache-control"] == "public, s-maxage=10, stale-while-revalidate=40"
        assert (await c.get("/api/terminal/quote")).status_code == 400
        many = ",".join(f"S{i}" for i in range(61))
        r = await c.get(f"/api/terminal/quote?symbols={many}")
        assert r.status_code == 400 and "max 60" in r.json()["error"]


async def test_chart_validation_and_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(symbol: str, range_key: str) -> dict[str, Any]:
        raise DataError("Yahoo returned HTTP 500")

    monkeypatch.setattr(ty, "get_chart", boom)
    async with client() as c:
        assert (await c.get("/api/terminal/chart?range=1M")).status_code == 400
        assert (await c.get("/api/terminal/chart?symbol=AAPL&range=2W")).status_code == 400
        r = await c.get("/api/terminal/chart?symbol=AAPL&range=1M")
        assert r.status_code == 502 and r.json() == {"error": "Yahoo returned HTTP 500"}


async def test_chart_cache_header_by_range(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(symbol: str, range_key: str) -> dict[str, Any]:
        return {"symbol": symbol, "candles": []}

    monkeypatch.setattr(ty, "get_chart", fake)
    async with client() as c:
        intraday = await c.get("/api/terminal/chart?symbol=AAPL&range=1D")
        daily = await c.get("/api/terminal/chart?symbol=AAPL&range=1Y")
        assert "s-maxage=30" in intraday.headers["cache-control"]
        assert "s-maxage=120" in daily.headers["cache-control"]


async def test_search_empty_is_ok_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    async with client() as c:
        r = await c.get("/api/terminal/search?q=")
        assert r.status_code == 200 and r.json() == []


async def test_news_and_market_and_fundamentals(monkeypatch: pytest.MonkeyPatch) -> None:
    async def news(symbol: str | None) -> list[dict[str, Any]]:
        return [{"title": f"news-for-{symbol}"}]

    async def market() -> dict[str, Any]:
        return {"indices": []}

    async def funda(symbol: str) -> dict[str, Any]:
        return {"symbol": symbol}

    monkeypatch.setattr(ty, "get_news", news)
    monkeypatch.setattr(ty, "get_market_overview", market)
    monkeypatch.setattr(ty, "get_fundamentals", funda)
    async with client() as c:
        assert (await c.get("/api/terminal/news")).json()[0]["title"] == "news-for-None"
        r = await c.get("/api/terminal/news?symbol=AAPL")
        assert r.json()[0]["title"] == "news-for-AAPL"
        assert "s-maxage=30" in r.headers["cache-control"]
        assert (await c.get("/api/terminal/market")).json() == {"indices": []}
        assert (await c.get("/api/terminal/fundamentals?symbol=AAPL")).json() == {
            "symbol": "AAPL"}
        assert (await c.get("/api/terminal/fundamentals")).status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_terminal_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: poseidon.terminal.routes`

- [ ] **Step 3: Implement `src/poseidon/terminal/routes.py`**

```python
"""Read-only market-study endpoints backing the embedded terminal UI.

Thin handlers: validation + envelope only; data logic lives in yahoo.py.
Contract: trading-terminal's lib/types.ts. Always call through the module
(`yahoo.fn`) so tests and the UI harness can monkeypatch.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..core.errors import DataError
from . import yahoo
from .constants import RANGE_CONFIG

router = APIRouter(prefix="/api/terminal")


def _ok(data: Any, s_maxage: int) -> JSONResponse:
    return JSONResponse(data, headers={
        "Cache-Control":
            f"public, s-maxage={s_maxage}, stale-while-revalidate={s_maxage * 4}",
    })


def _fail(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.get("/quote")
async def quote(symbols: str = "") -> JSONResponse:
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if not syms:
        return _fail("Missing `symbols` query parameter", 400)
    if len(syms) > 60:
        return _fail("Too many symbols (max 60)", 400)
    try:
        return _ok(await yahoo.get_quotes(syms), 10)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/chart")
async def chart(symbol: str = "", range: str = "1M") -> JSONResponse:  # noqa: A002
    sym, range_key = symbol.strip(), range.upper()
    if not sym:
        return _fail("Missing `symbol` query parameter", 400)
    if range_key not in RANGE_CONFIG:
        return _fail(f"Invalid range: {range_key}", 400)
    try:
        return _ok(await yahoo.get_chart(sym, range_key),
                   30 if range_key in ("1D", "5D") else 120)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/search")
async def search(q: str = "") -> JSONResponse:
    if not q.strip():
        return _ok([], 60)
    try:
        return _ok(await yahoo.search_symbols(q), 60)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/fundamentals")
async def fundamentals(symbol: str = "") -> JSONResponse:
    if not symbol.strip():
        return _fail("Missing `symbol` query parameter", 400)
    try:
        return _ok(await yahoo.get_fundamentals(symbol), 3600)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/news")
async def news(symbol: str | None = None) -> JSONResponse:
    try:
        return _ok(await yahoo.get_news(symbol), 30)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/market")
async def market() -> JSONResponse:
    try:
        return _ok(await yahoo.get_market_overview(), 15)
    except DataError as exc:
        return _fail(str(exc), 502)
```

(`noqa: A002` because ruff's `A` set flags the `range` parameter name — kept
to mirror the standalone query param exactly.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_terminal_routes.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check src tests && .venv/bin/mypy src
git add src/poseidon/terminal/routes.py tests/unit/test_terminal_routes.py
git commit -m "feat(terminal): /api/terminal routes with ts-contract envelopes"
```

---

### Task 8: Server wiring, auth exemption, nav entry

**Files:**
- Modify: `src/poseidon/api/server.py` (auth middleware + router include + mount)
- Modify: `src/poseidon/api/static/index.html` (nav entry)
- Modify: `docs/superpowers/specs/2026-07-09-trading-terminal-embed-design.md` (auth addendum)
- Test: `tests/unit/test_terminal_wiring.py`

**Interfaces:**
- Consumes: `poseidon.terminal.routes.router`.
- Produces: `/api/terminal/*` and `/terminal/*` live in `build_app()`; both exempt from the bearer-token middleware; bundle mount is skipped gracefully when the bundle dir is absent.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_terminal_wiring.py` — uses the same stub-kernel trick as
`tools/ui_verify.py` (import it for its `StubKernel` if importable, else
construct the minimal attributes; check ui_verify.py for the exact stub class
name and reuse by `sys.path` insertion the way ui_verify itself does). If
reusing the stub proves noisy, build the app and inspect routes statically:

```python
"""Terminal endpoints/mount are wired into build_app and token-exempt."""

from __future__ import annotations

from poseidon.terminal.routes import router


def test_router_paths_are_namespaced() -> None:
    paths = {r.path for r in router.routes}
    assert paths == {
        "/api/terminal/quote", "/api/terminal/chart", "/api/terminal/search",
        "/api/terminal/fundamentals", "/api/terminal/news", "/api/terminal/market",
    }


def test_server_source_wires_terminal() -> None:
    # The build_app factory needs a full kernel; assert wiring at source level
    # (cheap, dependency-free) — ui_verify covers the runtime path end-to-end.
    import pathlib

    src = pathlib.Path("src/poseidon/api/server.py").read_text(encoding="utf-8")
    assert "from ..terminal.routes import router as terminal_router" in src
    assert "app.include_router(terminal_router)" in src
    assert 'STATIC_DIR / "terminal"' in src
    assert '"/terminal", "/api/terminal"' in src  # auth exemption tuple


def test_nav_has_terminal_entry() -> None:
    import pathlib

    html = pathlib.Path("src/poseidon/api/static/index.html").read_text(encoding="utf-8")
    assert 'href="/terminal/"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_terminal_wiring.py -v`
Expected: `test_router_paths_are_namespaced` PASSES; the other two FAIL.

- [ ] **Step 3: Wire `server.py`**

Import (top, with the relative imports): `from ..terminal.routes import router as terminal_router`

In the `_require_token` middleware, replace the `/static` exemption line:

```python
            # /static and the embedded market-study terminal are token-exempt:
            # static assets carry no secrets and cannot send headers from
            # <script>/<link>; /terminal + /api/terminal are read-only public
            # market data (keyless Yahoo) — no account, positions, or broker
            # state is reachable through them (spec addendum 2026-07-09).
            if request.url.path.startswith(("/static", "/terminal", "/api/terminal")):
                return await call_next(request)
```

Just before `app.mount("/static", …)` (line ~733):

```python
    app.include_router(terminal_router)
    terminal_bundle = STATIC_DIR / "terminal"
    if terminal_bundle.is_dir():  # bundle is committed; guard keeps bare
        app.mount("/terminal",    # checkouts (or stripped builds) booting
                  StaticFiles(directory=terminal_bundle, html=True),
                  name="terminal")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
```

- [ ] **Step 4: Nav entry in `index.html`**

Insert before `</nav>` (after the last existing nav `<a>`), matching the
existing icon markup style:

```html
    <a href="/terminal/" title="Bloomberg-style market-study terminal">
      <svg viewBox="0 0 20 20"><path d="M5 3v4M3.5 4h3M5 9v8M10 6v3M8.5 12h3M10 12v5M10 3v3M15 4v6M13.5 6h3M15 13v4"/></svg>
      <span>Terminal</span></a>
```

(No `data-view` attribute — the SPA hash-router must ignore it; verify
`app.js` selects nav links by `[data-view]` before relying on this, and if it
binds all `nav a` add an early `if (!a.dataset.view) return;` guard there.)

- [ ] **Step 5: Spec addendum**

Append to the spec's Error handling section:

```markdown
**Auth (addendum, decided at implementation):** Poseidon's optional bearer
token exempts only `/static`. The embed extends the exemption to `/terminal`
and `/api/terminal` — GET-only, keyless public market data; no account,
position, or broker state flows through these paths. On a tokened
non-loopback deployment the terminal is therefore readable without the
token, which matches its data sensitivity (public quotes/news).
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/unit/test_terminal_wiring.py -v`
Expected: PASS (3 tests). Full suite: `.venv/bin/pytest -q` → green.

- [ ] **Step 7: Gate + commit**

```bash
.venv/bin/ruff check src tests && .venv/bin/mypy src
git add -u src docs && git add tests/unit/test_terminal_wiring.py
git commit -m "feat(terminal): wire routes + bundle mount + nav entry; token-exempt terminal paths"
```

---

### Task 9: Build and commit the bundle

**Files:**
- Create: `src/poseidon/api/static/terminal/` (generated assets)
- Create: `src/poseidon/api/static/terminal/BUNDLE.md`

- [ ] **Step 1: Generate**

```bash
cd ~/trading-terminal
EMBED_DEST=~/Poseidon/src/poseidon/api/static/terminal npm run build:embed
```

Expected: "Bundle synced to …/static/terminal".

- [ ] **Step 2: Provenance note**

`src/poseidon/api/static/terminal/BUNDLE.md`:

```markdown
# Embedded Trading Terminal bundle

Generated artifact — do not edit. Source: https://github.com/St3althWarri0r/Trading-Terminal
(commit recorded on the publish that produced this bundle; see Poseidon docs/terminal.md).

Regenerate:
    cd <Trading-Terminal checkout>
    EMBED_DEST=<Poseidon>/src/poseidon/api/static/terminal npm run build:embed
```

- [ ] **Step 3: Sanity-check the artifact**

```bash
ls ~/Poseidon/src/poseidon/api/static/terminal/           # index.html, _next/, BUNDLE.md
grep -c "/terminal/_next" ~/Poseidon/src/poseidon/api/static/terminal/index.html  # >= 1
du -sh ~/Poseidon/src/poseidon/api/static/terminal/       # sanity: ~0.5–2 MB
```

- [ ] **Step 4: Wheel packaging check**

```bash
cd ~/Poseidon && .venv/bin/python -m pip wheel . --no-deps -w /tmp/pw -q \
  && .venv/bin/python -c "
import glob, zipfile
w = glob.glob('/tmp/pw/poseidon-*.whl')[0]
names = zipfile.ZipFile(w).namelist()
assert any('api/static/terminal/index.html' in n for n in names), 'bundle missing from wheel'
print('bundle ships in wheel OK')"
```

Expected: `bundle ships in wheel OK`. If it FAILS (hatchling file selection
surprise), add the bundle to the existing force-include block in
`pyproject.toml` — `"src/poseidon/api/static/terminal" = "poseidon/api/static/terminal"`
— and rerun this step.

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/api/static/terminal
git commit -m "feat(terminal): commit embedded UI bundle (static export)"
```

---

### Task 10: UI harness checks

**Files:**
- Modify: `tools/ui_verify.py`

- [ ] **Step 1: Stub the terminal data layer**

In `tools/ui_verify.py`, after the existing imports/stubs, monkeypatch the
module functions (the routes call through `yahoo.<fn>`, so this is enough):

```python
import poseidon.terminal.yahoo as terminal_yahoo  # noqa: E402


def _stub_terminal_data() -> None:
    q = {"symbol": "AAPL", "name": "Apple Inc.", "quoteType": "EQUITY",
         "currency": "USD", "exchange": "NasdaqGS", "marketState": "REGULAR",
         "price": 314.66, "change": 1.25, "changePercent": 0.4,
         "previousClose": 313.39, "open": 310.45, "dayHigh": 315.5,
         "dayLow": 308.16, "volume": 26_390_000, "avgVolume": 54_440_000,
         "marketCap": 4.62e12, "trailingPE": 38.05, "forwardPE": None,
         "eps": 7.1, "dividendYield": 0.0044, "beta": 1.2,
         "fiftyTwoWeekHigh": 317.4, "fiftyTwoWeekLow": 201.5,
         "fiftyDayAverage": None, "twoHundredDayAverage": None,
         "sharesOutstanding": None, "postMarketPrice": None,
         "postMarketChange": None, "postMarketChangePercent": None,
         "preMarketPrice": None, "preMarketChange": None,
         "preMarketChangePercent": None}

    async def quotes(symbols: list[str]) -> list[dict]:
        return [dict(q, symbol=s, name=s) for s in symbols]

    async def market() -> dict:
        row = dict(q)
        return {"indices": [row], "futures": [row], "rates": [row],
                "commodities": [row], "crypto": [row], "currencies": [row],
                "sectors": [{"symbol": "XLK", "name": "Technology",
                             "changePercent": 1.9}]}

    async def chart(symbol: str, range_key: str) -> dict:
        candles = [{"time": 1_752_000_000 + i * 86_400, "open": 300.0 + i,
                    "high": 302.0 + i, "low": 299.0 + i, "close": 301.0 + i,
                    "volume": 1_000_000} for i in range(30)]
        return {"symbol": symbol, "currency": "USD", "exchangeName": "NasdaqGS",
                "regularMarketPrice": 314.66, "previousClose": 313.39,
                "candles": candles}

    async def news(symbol: str | None) -> list[dict]:
        return [{"id": "n1", "title": "Markets rally on strong earnings",
                 "publisher": "STUBWIRE", "link": "https://example.com",
                 "publishedAt": 1_752_000_000_000, "thumbnail": None,
                 "tickers": ["AAPL"]}]

    async def funda(symbol: str) -> dict:
        return {"symbol": symbol, "profile": {"name": symbol, "sector": "Tech",
                "industry": None, "employees": None, "country": None,
                "city": None, "website": None, "summary": None},
                "valuation": {}, "financials": {}, "perShare": {}, "targets": {}}

    terminal_yahoo.get_quotes = quotes  # type: ignore[assignment]
    terminal_yahoo.get_market_overview = market  # type: ignore[assignment]
    terminal_yahoo.get_chart = chart  # type: ignore[assignment]
    terminal_yahoo.get_news = news  # type: ignore[assignment]
    terminal_yahoo.get_fundamentals = funda  # type: ignore[assignment]
```

Call `_stub_terminal_data()` right before the harness builds the app.

- [ ] **Step 2: Add the checks**

In the Playwright section (after the existing view checks), following the
harness's `check()` idiom. `PORT` below stands for the harness's existing
base-URL/port variable — `grep -n "page.goto" tools/ui_verify.py` first and
reuse whatever it already uses:

```python
    # --- Embedded terminal ---
    resp = await page.goto(f"http://127.0.0.1:{PORT}/terminal/")
    check("terminal: bundle serves", resp is not None and resp.status == 200)
    await page.wait_for_timeout(2500)  # boot splash -> live terminal
    body = await page.inner_text("body")
    check("terminal: shell rendered", "TRADING TERMINAL" in body or "WATCHLIST" in body.upper())
    check("terminal: market data rendered", "AAPL" in body)
    api = await page.evaluate(
        "fetch('/api/terminal/market').then(r => r.json())")
    check("terminal: market endpoint shape",
          isinstance(api, dict) and set(api) >= {"indices", "sectors"})
    await page.screenshot(path=f"{SHOTS}/terminal.png", full_page=True)
```

Also add a nav check in the dashboard section:

```python
    check("nav: terminal entry present",
          await page.locator('nav a[href="/terminal/"]').count() == 1)
```

- [ ] **Step 3: Run the harness**

Run: `cd ~/Poseidon && .venv/bin/python tools/ui_verify.py`
Expected: all existing checks + the 5 new ones print PASS; exit code 0.
(If chromium is missing: `PW_CHROMIUM=~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome` is available on this machine.)

- [ ] **Step 4: Gate + commit**

```bash
.venv/bin/ruff check tools && .venv/bin/mypy src
git add tools/ui_verify.py
git commit -m "test(terminal): ui_verify covers /terminal bundle + endpoint shape"
```

---

### Task 11: Live smoke test, docs, version 2.6.0, full gate

**Files:**
- Create: `tests/integration/test_terminal_live.py`
- Create: `docs/terminal.md`
- Modify: `README.md`, `pyproject.toml`, `src/poseidon/__init__.py`, `packaging/PKGBUILD`

- [ ] **Step 1: Env-gated live smoke**

`tests/integration/test_terminal_live.py`:

```python
"""Opt-in live Yahoo smoke: POSEIDON_LIVE_TESTS=1 pytest tests/integration/test_terminal_live.py"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSEIDON_LIVE_TESTS"),
    reason="live Yahoo test; set POSEIDON_LIVE_TESTS=1 to run",
)


async def test_live_quote_and_chart() -> None:
    from poseidon.terminal import yahoo

    quotes = await yahoo.get_quotes(["AAPL"])
    assert quotes and quotes[0]["symbol"] == "AAPL" and quotes[0]["price"] is not None

    chart = await yahoo.get_chart("AAPL", "1M")
    assert len(chart["candles"]) > 5
    times = [c["time"] for c in chart["candles"]]
    assert times == sorted(set(times))  # strictly ascending, deduped
```

Run once now: `POSEIDON_LIVE_TESTS=1 .venv/bin/pytest tests/integration/test_terminal_live.py -v`
Expected: PASS (proves the crumb dance against real Yahoo). Then confirm the
default run skips it: `.venv/bin/pytest tests/integration/test_terminal_live.py -v` → SKIPPED.

- [ ] **Step 2: `docs/terminal.md`**

```markdown
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
```

- [ ] **Step 3: README bullet**

Add to the features list in `README.md` (match surrounding style):

```markdown
- **Market-study terminal** — the full Bloomberg-style Trading-Terminal
  (charts, fundamentals, news, watchlist) embedded at `/terminal`, served
  natively by Poseidon with keyless Yahoo data (`docs/terminal.md`).
```

- [ ] **Step 4: Version 2.6.0**

- `pyproject.toml`: `version = "2.6.0"`
- `src/poseidon/__init__.py`: `__version__ = "2.6.0"`
- `packaging/PKGBUILD`: per CLAUDE.md the PKGBUILD *derives* `pkgver` from
  `pyproject.toml` — verify with `grep -n "pkgver" packaging/PKGBUILD`; only
  edit if a hard-coded version appears there.

- [ ] **Step 5: Full gate**

```bash
cd ~/Poseidon
.venv/bin/ruff check src tests tools
.venv/bin/mypy src
.venv/bin/pytest -q
.venv/bin/python tools/ui_verify.py
```

Expected: all green (pytest count grows by the ~27 new tests; live test skipped).

- [ ] **Step 6: Commit**

```bash
git add -u && git add tests/integration/test_terminal_live.py docs/terminal.md
git commit -m "docs+release(terminal): live smoke, terminal docs, v2.6.0"
```

---

### Task 12: Publish Trading-Terminal changes (GitHub MCP)

The Trading-Terminal repo has no local git. Publish the Task 1 edits to
`St3althWarri0r/Trading-Terminal` `main` with the GitHub MCP `push_files`
(house technique: full-file contents from in-session Reads, then blob-SHA
verify — see memory note `github-publishing-technique`).

- [ ] **Step 1: Push** `package.json`, `next.config.ts`, `lib/api-client.ts`,
  `scripts/build-embed.sh`, `README.md` in one commit:
  `"v0.3.0: embeddable static-export build (Poseidon)"` — bump `version` to
  `0.3.0` in package.json as part of the same push.
- [ ] **Step 2: Verify** — `git hash-object <each file>` locally must equal the
  `sha` from `get_file_contents` directory listings for all 5 files.
- [ ] **Step 3: Record provenance** — take the new commit SHA, append
  `Built from Trading-Terminal commit <sha>.` to
  `~/Poseidon/src/poseidon/api/static/terminal/BUNDLE.md`, then:

```bash
cd ~/Poseidon && git add src/poseidon/api/static/terminal/BUNDLE.md
git commit -m "chore(terminal): record bundle provenance"
```

---

### Done — NOT in this plan

- Poseidon release mechanics (PR "Poseidon 2.6.0 — Embedded Trading Terminal (#N)",
  merge, tag, GitHub release): needs the user's classic push token per the repo's
  auth setup — ask the user when the branch is ready.
- Portfolio overlay in the terminal (phase 2, per spec).
