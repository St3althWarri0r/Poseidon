# Paper Dry Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a transparent "Dry Run" dashboard view that lets the operator run Claude + the built-in algorithms against the paper account risk-free, by wiring existing capabilities together plus one read-only aggregation endpoint.

**Architecture:** One pure summarizer function + a thin `GET /api/dryrun` endpoint feed a new "Dry Run" left-nav view. The view's toggles reuse existing action endpoints (`/api/brokers/connect`, `/api/algorithms/{id}/activate`, `/api/mode`, `/api/cycle`). A small framing fix stops the autonomous-mode confirm from warning about "real money" on the paper broker. Tests: one pytest for the pure summarizer, plus the browser harness (`tools/ui_verify.py`) for the view.

**Tech Stack:** Python 3.11 / FastAPI (backend), vanilla JS + HTML/CSS (dashboard), Playwright (`tools/ui_verify.py` UI harness), pytest.

## Global Constraints

- Python `>=3.11`; ruff + mypy(strict) must stay clean; run via `.venv/bin/` (`source .venv/bin/activate`).
- No new runtime dependencies. Reuse existing endpoints for all state changes; only `GET /api/dryrun` is new backend surface.
- Follow existing patterns: endpoints are `@app.<verb>("/api/...")` async functions inside `build_app`; frontend views are a `VIEWS` registry entry + a `<section class="view" data-view="...">` + `refresh*/render*` functions in `app.js`; toasts for errors.
- Safety invariant: the Dry Run view only ever sets Autonomous while the active broker is paper; "Stop dry run" sets Research mode. No changes to fills/risk/execution.
- Version bump to `2.5.0`; keep README/dev-guide test counts and `pyproject.toml`/`__init__.py`/`PKGBUILD` in sync.

---

### Task 1: Backend — `/api/dryrun` state (pure summarizer + endpoint)

**Files:**
- Modify: `src/poseidon/strategy/workshop.py` (extract the bundled-seed marker to a module constant)
- Modify: `src/poseidon/api/server.py` (add `build_dryrun_state` pure function + `GET /api/dryrun`)
- Test: `tests/unit/test_dryrun_endpoint.py`

**Interfaces:**
- Produces: `poseidon.strategy.workshop.BUNDLED_REVIEW_NOTE: str`
- Produces: `poseidon.api.server.build_dryrun_state(*, broker_is_paper: bool, active_broker: str, mode_value: str, algorithms_raw: list[dict], session: MarketSession) -> dict` — returns the `/api/dryrun` payload.
- Consumes (endpoint): `kernel.broker.is_paper`, `kernel.broker.name`, `kernel.order_manager.mode.value`, `kernel.workshop.list_all()`, `kernel.clock.session()`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_dryrun_endpoint.py`:

```python
"""Unit tests for the Dry Run state summarizer (GET /api/dryrun)."""

from __future__ import annotations

from poseidon.api.server import build_dryrun_state
from poseidon.core.enums import MarketSession
from poseidon.strategy.workshop import BUNDLED_REVIEW_NOTE


def _algo(id_: str, status: str, *, bundled: bool = False) -> dict:
    return {"id": id_, "name": f"algo-{id_}", "status": status,
            "review_notes": BUNDLED_REVIEW_NOTE if bundled else ""}


def test_dryrun_state_counts_and_flags() -> None:
    state = build_dryrun_state(
        broker_is_paper=True, active_broker="paper", mode_value="research",
        algorithms_raw=[_algo("a", "active", bundled=True),
                        _algo("b", "draft", bundled=True),
                        _algo("c", "draft", bundled=False)],
        session=MarketSession.REGULAR,
    )
    assert state["broker_is_paper"] is True
    assert state["active_broker"] == "paper"
    assert state["mode"] == "research"
    assert state["active_algo_count"] == 1
    assert state["bundled_draft_count"] == 1  # 'b' only (c is not bundled)
    assert state["market"] == {"session": "regular", "is_open": True, "opens_hint": None}
    ids = {a["id"]: a for a in state["algorithms"]}
    assert ids["a"]["bundled"] is True and ids["c"]["bundled"] is False


def test_dryrun_state_market_closed() -> None:
    state = build_dryrun_state(
        broker_is_paper=False, active_broker="alpaca", mode_value="autonomous",
        algorithms_raw=[], session=MarketSession.CLOSED,
    )
    assert state["market"] == {"session": "closed", "is_open": False, "opens_hint": "9:30 ET"}
    assert state["algorithms"] == [] and state["active_algo_count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/unit/test_dryrun_endpoint.py -q`
Expected: FAIL — `ImportError: cannot import name 'BUNDLED_REVIEW_NOTE'` / `build_dryrun_state`.

- [ ] **Step 3a: Extract the seed marker constant**

In `src/poseidon/strategy/workshop.py`, add a module-level constant near the top (after the imports/module docstring) and use it in `seed_bundled`:

```python
# The review note stamped on bundled example algorithms when first seeded.
# The dashboard's Dry Run panel uses it to recognise the built-in starters.
BUNDLED_REVIEW_NOTE = "bundled example — review before activating"
```

Then in `seed_bundled`, replace the literal:

```python
                await self.create(name=path.stem, source=source,
                                  description=first_line,
                                  review_notes=BUNDLED_REVIEW_NOTE)
```

- [ ] **Step 3b: Add the pure summarizer + endpoint**

In `src/poseidon/api/server.py`, add the module-level pure function (top level, near the other module helpers, NOT inside `build_app`):

```python
def build_dryrun_state(*, broker_is_paper: bool, active_broker: str, mode_value: str,
                       algorithms_raw: list[dict[str, Any]], session: "MarketSession") -> dict[str, Any]:
    """Aggregate the Dry Run panel's state from plain inputs (pure, testable)."""
    from ..strategy.workshop import BUNDLED_REVIEW_NOTE
    from ..core.enums import MarketSession
    algorithms = [
        {"id": a["id"], "name": a["name"], "status": a["status"],
         "bundled": a.get("review_notes") == BUNDLED_REVIEW_NOTE}
        for a in algorithms_raw
    ]
    is_open = session is MarketSession.REGULAR
    return {
        "broker_is_paper": broker_is_paper,
        "active_broker": active_broker,
        "mode": mode_value,
        "algorithms": algorithms,
        "active_algo_count": sum(1 for a in algorithms if a["status"] == "active"),
        "bundled_draft_count": sum(1 for a in algorithms
                                   if a["bundled"] and a["status"] == "draft"),
        "market": {"session": session.value, "is_open": is_open,
                   "opens_hint": None if is_open else "9:30 ET"},
    }
```

Add the endpoint inside `build_app` next to the other broker endpoints (e.g. just after `@app.post("/api/brokers/schwab/exchange")`):

```python
    @app.get("/api/dryrun")
    async def dryrun_state() -> JSONResponse:
        """Everything the Dry Run panel needs, in one read."""
        return JSONResponse(build_dryrun_state(
            broker_is_paper=kernel.broker.is_paper,
            active_broker=kernel.broker.name,
            mode_value=kernel.order_manager.mode.value,
            algorithms_raw=await kernel.workshop.list_all(),
            session=kernel.clock.session(),
        ))
```

If `MarketSession` is not already imported at the top of `server.py`, the `build_dryrun_state` local import covers runtime; add `from ..core.enums import MarketSession` under `TYPE_CHECKING` only if mypy needs the annotation (otherwise keep the string annotation as written).

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/unit/test_dryrun_endpoint.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Gate + commit**

```bash
source .venv/bin/activate && ruff check src tests && mypy src && python -m pytest -q
git add src/poseidon/strategy/workshop.py src/poseidon/api/server.py tests/unit/test_dryrun_endpoint.py
git commit -m "feat(dryrun): add /api/dryrun state summarizer + endpoint"
```

---

### Task 2: Harness — FakeKernel serves `/api/dryrun` with a bundled starter

**Files:**
- Modify: `tools/ui_verify.py` (FakeWorkshop gets a bundled draft; FakeKernel already exposes broker/order_manager/workshop/clock)

**Interfaces:**
- Consumes: `build_app` already routes `GET /api/dryrun` to the real endpoint, which reads `kernel.workshop.list_all()` etc. — FakeKernel already provides these. Only data needs adjusting so the panel has a bundled starter to activate.

- [ ] **Step 1: Give the fake workshop a bundled starter draft**

In `tools/ui_verify.py`, in `FakeWorkshop.rows`, change the `a2` (`tqqq_day_trader`, draft) row's `review_notes` to the bundled marker so the Dry Run panel recognises it as a startable built-in:

```python
             "review_notes": "bundled example — review before activating", "sleeve_pct": 0,
```

(Keep the string literal here — `tools/` avoids importing app internals for fixtures; it mirrors `BUNDLED_REVIEW_NOTE`.)

- [ ] **Step 2: Sanity-check the endpoint serves under the harness**

Run: `source .venv/bin/activate && python tools/ui_verify.py`
Expected: PASS (unchanged 31 checks — the new endpoint is served but not yet asserted).

- [ ] **Step 3: Commit**

```bash
git add tools/ui_verify.py
git commit -m "test(dryrun): fake workshop exposes a bundled starter for the harness"
```

---

### Task 3: Frontend — the "Dry Run" view

**Files:**
- Modify: `src/poseidon/api/static/index.html` (nav item + view section)
- Modify: `src/poseidon/api/static/app.js` (VIEWS entry, `refreshDryRun`, `renderDryRun`, action handlers)
- Modify: `src/poseidon/api/static/style.css` (a couple of small classes; reuse existing where possible)
- Modify: `tools/ui_verify.py` (Dry Run walkthrough checks)

**Interfaces:**
- Consumes: `GET /api/dryrun` (Task 1); existing `POST /api/brokers/connect`, `POST /api/algorithms/{id}/activate`, `POST /api/mode`, `POST /api/cycle`; existing `$`, `$$`, `getJSON`, `postJSON`, `toast`, `esc` helpers in `app.js`.

- [ ] **Step 1: Add the failing harness checks**

In `tools/ui_verify.py`, after the Account-view block (right before the Algorithms/auto-invest block) add:

```python
        # -- Dry Run: the guided paper-dry-run panel -----------------------
        await page.click('a[data-view="dryrun"]')
        await page.wait_for_timeout(400)
        banner = await page.text_content("#dryrun-banner")
        check("dry run safe banner", banner is not None and "no real money" in banner.lower(),
              repr(banner))
        rows = await page.locator("#dryrun-steps .dryrun-step").count()
        check("dry run has three steps", rows == 3, f"count={rows}")
        market = await page.text_content("#dryrun-market")
        check("dry run market indicator", market is not None and "market" in market.lower(),
              repr(market))
        # flip autonomous on (paper broker -> no real-money confirm needed)
        await page.click("#dryrun-mode-toggle")
        await page.wait_for_timeout(400)
        mode_state = await page.text_content("#dryrun-mode-state")
        check("dry run autonomous engaged", mode_state is not None
              and "autonomous" in mode_state.lower(), repr(mode_state))
        # run a cycle now
        await page.click("#dryrun-run-now")
        await page.wait_for_timeout(300)
        toasts = await page.text_content("#toasts")
        check("dry run run-now", toasts is not None and "cycle" in toasts.lower(), repr(toasts))
        # stop -> research
        await page.click("#dryrun-stop")
        await page.wait_for_timeout(400)
        mode_state = await page.text_content("#dryrun-mode-state")
        check("dry run stop -> research", mode_state is not None
              and "research" in mode_state.lower(), repr(mode_state))
        await page.screenshot(path=f"{SHOTS}/v-dryrun.png")
```

Run: `source .venv/bin/activate && python tools/ui_verify.py`
Expected: FAIL — `dry run safe banner` (and following) fail: the view does not exist yet.

- [ ] **Step 2: Add the nav item + view section (index.html)**

In `src/poseidon/api/static/index.html`, add a nav link after the Account link (mirroring the existing `<a>` pattern):

```html
    <a href="#/dryrun" data-view="dryrun">
      <svg viewBox="0 0 20 20"><path d="M4 10a6 6 0 1 1 12 0 6 6 0 0 1-12 0M10 6v4l3 2"/></svg>
      <span>Dry Run</span></a>
```

And add the view section after the `account` section (before `risk`):

```html
  <section class="view" data-view="dryrun" hidden>
    <p class="meter-note good" id="dryrun-banner">PAPER — no real money. This is a safe simulation of the full autonomous stack.</p>
    <div id="dryrun-steps"></div>
    <p class="meter-note" id="dryrun-market"></p>
    <div class="ticket-actions">
      <button type="button" class="btn" id="dryrun-run-now">Run a review cycle now</button>
      <button type="button" class="btn btn-ghost" id="dryrun-stop">Stop dry run (back to Research)</button>
    </div>
    <p class="meter-note" id="dryrun-summary"></p>
  </section>
```

- [ ] **Step 3: Register the view + render/refresh (app.js)**

In `src/poseidon/api/static/app.js`, add to the `VIEWS` registry:

```javascript
  dryrun:      { title: "Dry Run",     refresh: () => refreshDryRun() },
```

Add the refresh/render functions (near the other `refresh*` functions):

```javascript
async function refreshDryRun() {
  let s;
  try { s = await getJSON("/api/dryrun"); }
  catch (e) { $("#dryrun-summary").textContent = "Could not load dry-run state: " + e.message; return; }
  renderDryRun(s);
}

function renderDryRun(s) {
  const on = (ok) => ok ? "✅" : "▫️";
  const brokerOk = s.broker_is_paper;
  const algosOk = s.active_algo_count > 0;
  const autoOk = s.mode === "autonomous";
  $("#dryrun-steps").innerHTML = `
    <div class="form-row dryrun-step"><span>${on(brokerOk)} Broker = Paper simulator</span>
      <button type="button" class="btn btn-ghost" id="dryrun-broker-toggle" ${brokerOk ? "disabled" : ""}>
        ${brokerOk ? "Active" : "Switch to paper"}</button>
      <small>${brokerOk ? "The safe simulator is active." : "Currently: " + esc(s.active_broker) + ". Switch to paper to dry-run safely."}</small></div>
    <div class="form-row dryrun-step"><span>${on(algosOk)} Built-in algorithms active (${s.active_algo_count} on)</span>
      <button type="button" class="btn btn-ghost" id="dryrun-algos-activate" ${s.bundled_draft_count ? "" : "disabled"}>
        ${s.bundled_draft_count ? "Activate the " + s.bundled_draft_count + " built-in algorithm(s)" : "None pending"}</button>
      <small>Their signals feed each review cycle alongside Claude.</small></div>
    <div class="form-row dryrun-step"><span id="dryrun-mode-state">${on(autoOk)} Autonomous mode (${esc(s.mode)})</span>
      <button type="button" class="btn btn-ghost" id="dryrun-mode-toggle" ${brokerOk ? "" : "disabled"}>
        ${autoOk ? "On" : "Turn on"}</button>
      <small>${brokerOk ? "Safe on paper — Claude executes its own trades." : "Switch to paper first."}</small></div>`;
  const m = s.market;
  $("#dryrun-market").textContent = m.is_open
    ? "Market open — paper trades can execute now."
    : `Market closed — the dry run will start trading at the next open (${m.opens_hint}).`;
  $("#dryrun-summary").textContent = (brokerOk && algosOk && autoOk)
    ? "✅ Dry run active — Claude and your algorithms are trading the paper account."
    : "Turn on all three steps above to start the dry run.";
  $("#dryrun-broker-toggle")?.addEventListener("click", dryrunSwitchToPaper);
  $("#dryrun-algos-activate")?.addEventListener("click", () => dryrunActivateStarters(s));
  $("#dryrun-mode-toggle")?.addEventListener("click", () => dryrunSetMode(autoOk ? "research" : "autonomous"));
}

async function dryrunSwitchToPaper() {
  try { await postJSON("/api/brokers/connect", { name: "paper", paper: true }); toast("Switched to the paper simulator", "good"); }
  catch (e) { toast("Could not switch to paper: " + e.message, "bad"); }
  refreshDryRun();
}

async function dryrunActivateStarters(s) {
  const starters = (s.algorithms || []).filter((a) => a.bundled && a.status === "draft");
  for (const a of starters) {
    try { await postJSON(`/api/algorithms/${a.id}/activate`, {}); }
    catch (e) { toast(`Could not activate ${a.name}: ${e.message}`, "bad"); }
  }
  toast(`Activated ${starters.length} built-in algorithm(s)`, "good");
  refreshDryRun();
}

async function dryrunSetMode(mode) {
  try { await postJSON("/api/mode", { mode }); toast("Mode: " + mode, mode === "autonomous" ? "warn" : "good"); }
  catch (e) { toast("Mode change failed: " + e.message, "bad"); }
  refreshDryRun();
}
```

Wire the two static buttons once (near the other `addEventListener` calls at the bottom of `app.js`):

```javascript
$("#dryrun-run-now").addEventListener("click", () =>
  postJSON("/api/cycle").then(() => toast("Review cycle started"))
    .catch((e) => toast("Review cycle failed: " + e.message, "bad")));
$("#dryrun-stop").addEventListener("click", () => dryrunSetMode("research"));
```

- [ ] **Step 4: Minimal styles (style.css)**

In `src/poseidon/api/static/style.css`, append:

```css
.dryrun-step { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.dryrun-step > span { min-width: 16rem; font-weight: 600; }
```

- [ ] **Step 5: Run the harness to verify it passes**

Run: `source .venv/bin/activate && python tools/ui_verify.py`
Expected: PASS — all prior checks plus the six new "dry run …" checks.

- [ ] **Step 6: Commit**

```bash
git add src/poseidon/api/static/index.html src/poseidon/api/static/app.js src/poseidon/api/static/style.css tools/ui_verify.py
git commit -m "feat(dryrun): guided Dry Run dashboard view"
```

---

### Task 4: Framing fix — no "real money" warning on paper

**Files:**
- Modify: `src/poseidon/api/static/app.js` (the `#mode-seg` autonomous confirm + `updateLiveWarning`)
- Modify: `tools/ui_verify.py` (capture the confirm dialog text)

**Interfaces:**
- Consumes: existing `#mode-seg` handler and `lastStatus.broker.paper` from `/api/status`.

- [ ] **Step 1: Add the failing harness assertion**

The harness auto-accepts dialogs. Capture their text. Near the top of the harness run (where `page.on("dialog", ...)` is set), record messages:

```python
        dialog_messages: list[str] = []
        page.on("dialog", lambda d: (dialog_messages.append(d.message), asyncio.ensure_future(d.accept())))
```

(If a `page.on("dialog", ...)` handler already exists, replace it with this capturing version.) Then, in the Overview/topbar section where the mode segment is exercised (or add a small block after status loads), assert switching to autonomous on the paper broker does not warn about real money:

```python
        dialog_messages.clear()
        await page.click('#mode-seg button[data-mode="autonomous"]')
        await page.wait_for_timeout(300)
        joined = " ".join(dialog_messages).lower()
        check("paper autonomous confirm omits 'real money'",
              "autonomous" in joined and "real money" not in joined, repr(dialog_messages))
```

Run: `source .venv/bin/activate && python tools/ui_verify.py`
Expected: FAIL — current confirm text includes "real money" unconditionally-ish, or the FakeKernel broker is paper and the message still references it. (If the FakeBroker is paper and the current code already omits it, adjust the FakeKernel to live first — see Step 2.)

- [ ] **Step 2: Make the warning live-only (app.js)**

In `src/poseidon/api/static/app.js`, in the `#mode-seg` click handler, the confirm should only add the real-money note when the active broker is live. Confirm the branch reads:

```javascript
    const broker = (lastStatus && lastStatus.broker) || {};
    const liveNote = broker.paper === false
      ? `\n\nACTIVE BROKER IS LIVE (${broker.name}) — trades will use real money.` : "";
```

If `broker.paper` is not present on `/api/status`, use the paper flag the status already exposes; otherwise fall back to treating a broker named `paper` as paper:

```javascript
    const isPaper = broker.paper === true || broker.name === "paper";
    const liveNote = isPaper ? "" : `\n\nACTIVE BROKER IS LIVE (${broker.name || "?"}) — trades will use real money.`;
```

Ensure `updateLiveWarning()` uses the same `isPaper` logic so the header banner matches.

- [ ] **Step 3: Ensure `/api/status` exposes the broker's paper flag**

First check what `GET /api/status` returns for `broker` (`grep -n '"broker"' src/poseidon/api/server.py` in the status endpoint). If it does not already include a paper indicator, add a minimal object to the status payload:

```python
            "broker": {"name": kernel.broker.name, "paper": kernel.broker.is_paper},
```

Then in `tools/ui_verify.py`, confirm the FakeKernel `/api/status` payload includes `broker: {"name": "paper", "paper": True}` (add the `paper` key if missing) so the assertion exercises the paper path. (`FakeBroker.is_paper` is already `True`.)

- [ ] **Step 4: Run the harness to verify it passes**

Run: `source .venv/bin/activate && python tools/ui_verify.py`
Expected: PASS including "paper autonomous confirm omits 'real money'".

- [ ] **Step 5: Commit**

```bash
git add src/poseidon/api/static/app.js tools/ui_verify.py
git commit -m "fix(dryrun): don't warn about real money when the broker is paper"
```

---

### Task 5: Docs, version bump, final gate, release

**Files:**
- Modify: `docs/user-guide.md` (a "Paper dry run" section)
- Modify: `pyproject.toml`, `src/poseidon/__init__.py`, `packaging/PKGBUILD` (2.4.1 → 2.5.0)
- Modify: `README.md`, `docs/developer-guide.md` (test count)

- [ ] **Step 1: Document the feature**

In `docs/user-guide.md`, add a short section:

```markdown
## Paper dry run

Before trading real money, run the whole autonomous stack — Claude and the
built-in algorithms — against the **paper** account, risk-free. Open the
**Dry Run** view and turn on its three steps: Paper broker, built-in
algorithms, and Autonomous mode. Use **Run a review cycle now** to trigger
Claude on demand (trades only fill during market hours). **Stop dry run**
returns the platform to Research mode. Nothing here can touch real money.
```

- [ ] **Step 2: Bump version + test count**

```bash
cd /home/shuffman95/Poseidon && source .venv/bin/activate
COUNT=$(python -m pytest 2>&1 | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
sed -i 's/^version = "2.4.1"/version = "2.5.0"/' pyproject.toml
sed -i 's/^__version__ = "2.4.1"/__version__ = "2.5.0"/' src/poseidon/__init__.py
sed -i 's/^pkgver=2.4.1/pkgver=2.5.0/' packaging/PKGBUILD
sed -i "s/# [0-9]\+ tests, a few seconds/# $COUNT tests, a few seconds/" docs/developer-guide.md
sed -i "s/\*\*Testing\*\* — [0-9]\+ unit\/integration tests/**Testing** — $COUNT unit\/integration tests/" README.md
```

- [ ] **Step 3: Full gate**

Run: `source .venv/bin/activate && ruff check src tests && mypy src && python -m pytest -q && python tools/ui_verify.py`
Expected: ruff clean, mypy clean, all pytest pass, UI harness "ALL CHECKS PASSED".

- [ ] **Step 4: Commit + open/merge PR + tag**

```bash
git add -A && git commit -m "docs+release: Paper Dry Run (v2.5.0)"
git push -u origin claude/paper-dry-run
```

Then open a PR into `main`, merge (merge commit titled `Poseidon 2.5.0 — Paper Dry Run (#N)`), tag `v2.5.0`, and cut the GitHub release. (Uses the write-enabled token; scrub any token from `.git/config` after push.)

---

## Notes for the implementer

- The `bundled` detection relies on the seed review-note (`BUNDLED_REVIEW_NOTE`). On a fresh install the only drafts are the bundled starters, so this is reliable; a future `origin` column could make it first-class (out of scope).
- Do not add new state-changing endpoints — the view composes existing ones. The only new endpoint is the read-only `GET /api/dryrun`.
- Keep the safety invariant visible in review: the view never flips a *live* broker to autonomous, and Stop always returns to Research.
