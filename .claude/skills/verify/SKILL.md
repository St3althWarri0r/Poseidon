---
name: verify
description: Verify Poseidon dashboard changes end-to-end in a real browser (Playwright over the real build_app + static assets with a stub kernel).
---

# Verifying Poseidon

The dashboard is the surface. Never trust "tests pass" for UI changes —
drive the rendered app.

## Recipe

```bash
pip install playwright && playwright install chromium   # once
python tools/ui_verify.py                               # ~30s, exits 1 on failure
# In environments with a preinstalled Playwright chromium:
#   PW_CHROMIUM=/opt/pw-browsers/chromium-*/chrome-linux/chrome python tools/ui_verify.py
```

The script serves the REAL `build_app()` (and the real static JS/CSS with
`__V__` version stamping) over `FakeKernel` — a stub with plausible
portfolio/orders/approvals/algorithms/broker data — then drives every view
with Playwright, asserting the load-bearing UI elements and interactions
(broker tiles + credential forms, chat round-trip, position Close prefill,
notional readout, Fill column, working-order count, automation tile,
auto-invest flow, approval countdown ticking) and screenshotting each view
into `tools/ui-verify-shots/`.

## Gotchas

- Add new endpoints to `FakeKernel` when `build_app` grows kernel
  dependencies — grep `kernel\.` in `src/poseidon/api/server.py`.
- `page.on("dialog", accept)` is pre-wired: confirm() dialogs (live-broker,
  reset, auto-invest) auto-accept.
- The engine cannot run for real here (vault + live data providers), so
  backend behavior is covered by pytest; this harness owns the UI seam.
