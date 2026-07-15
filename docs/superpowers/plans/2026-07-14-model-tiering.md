# Model Tiering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run auxiliary AI roles (chat, reflection) on a cheap/fast "utility" model while the trading decision (and the tool-forcing reviewer) stay on the strong primary model.

**Architecture:** One optional config field `ai.utility_model`. A `build_backends()` helper returns `(primary, utility)` where utility is the same object as primary unless a utility model is configured (same backend/endpoint, model swapped). `app.py` routes chat + reflection to the utility backend; agent + reviewer keep the primary.

**Tech Stack:** pydantic v2 (`AIConfig`), the existing `ChatBackend` seam + `build_backend`, pytest.

## Global Constraints

- Python 3.11+, `from __future__ import annotations`, mypy `--strict`, ruff line length 100.
- **Invariant:** the trading agent (`ClaudeAgent`, `run_cycle`) is ALWAYS given the primary backend — never the utility one.
- Backward-compatible: no `utility_model` ⇒ `utility is primary` (one backend, behavior unchanged).
- No touch to the decision/risk/order/audit path.
- Gate: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest`.

---

### Task 1: `ai.utility_model` config + `build_backends` helper

**Files:**
- Modify: `src/poseidon/core/config.py` (add a field to `AIConfig`, ~line 58)
- Modify: `src/poseidon/ai/backends/__init__.py` (add `build_backends`)
- Test: `tests/unit/test_backend_tiering.py`

**Interfaces:**
- Consumes: `build_backend(cfg: AIConfig, resolve_secret) -> ChatBackend` (existing); backends expose `.model`.
- Produces: `AIConfig.utility_model: str | None`; `build_backends(cfg: AIConfig, resolve_secret: Callable[[str], str]) -> tuple[ChatBackend, ChatBackend]` returning `(primary, utility)`.

- [ ] **Step 1: Write the failing test**
```python
# tests/unit/test_backend_tiering.py
from __future__ import annotations

from poseidon.ai.backends import build_backends
from poseidon.core.config import AIConfig


def _cfg(**kw) -> AIConfig:
    return AIConfig(backend="openai_compatible", base_url="http://x/v1", model="big", **kw)


def test_no_utility_model_returns_primary_twice() -> None:
    primary, utility = build_backends(_cfg(), lambda k: "")
    assert utility is primary                      # one backend, no tiering


def test_utility_model_builds_a_distinct_backend() -> None:
    primary, utility = build_backends(_cfg(utility_model="small"), lambda k: "")
    assert utility is not primary
    assert primary.model == "big" and utility.model == "small"


def test_utility_model_defaults_none() -> None:
    assert AIConfig().utility_model is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_backend_tiering.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_backends'`.

- [ ] **Step 3: Write minimal implementation**

In `src/poseidon/core/config.py`, inside `AIConfig` (after `reflection`, before the `@model_validator`):
```python
    # Cheap/fast "utility" model for auxiliary roles (chat, reflection). Same
    # backend + endpoint as the primary, model swapped. None = no tiering (all
    # roles use the primary). The trading decision always uses the primary.
    utility_model: str | None = None
```
In `src/poseidon/ai/backends/__init__.py`, add (below `build_backend`):
```python
def build_backends(cfg: AIConfig,
                   resolve_secret: Callable[[str], str]) -> tuple[ChatBackend, ChatBackend]:
    """(primary, utility) backends. Utility IS the primary object unless
    cfg.utility_model is set, in which case it is the same backend/endpoint with
    the model swapped — for the tolerant auxiliary roles (chat, reflection). The
    trading decision must always use the primary."""
    primary = build_backend(cfg, resolve_secret)
    if not cfg.utility_model:
        return primary, primary
    utility = build_backend(cfg.model_copy(update={"model": cfg.utility_model}), resolve_secret)
    return primary, utility
```
Ensure `Callable` is imported in `__init__.py` (`from collections.abc import Callable`) and `AIConfig`/`ChatBackend` are in scope (they are — `build_backend` already uses them).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_backend_tiering.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**
```bash
git add src/poseidon/core/config.py src/poseidon/ai/backends/__init__.py tests/unit/test_backend_tiering.py
git commit -m "feat(ai): ai.utility_model + build_backends helper for model tiering"
```

---

### Task 2: Route chat + reflection to the utility backend

**Files:**
- Modify: `src/poseidon/app.py` (backend build ~line 176; reflection `get_backend` ~181; `ChatService` ~196; `aclose` ~1110)
- Modify: `config/poseidon.example.yaml`, `docs/api-configuration.md`
- Test: `tests/unit/test_backend_tiering.py` (add a plumbing assertion)

**Interfaces:**
- Consumes: `build_backends` (Task 1); `ClaudeAgent(cfg, backend, dispatcher)` with a read-only `.backend`; `ChatService(cfg, backend, dispatcher, db)`; `ReflectionService(..., get_backend=...)`.
- Produces: `kernel._backend` (primary) + `kernel._utility_backend`; chat/reflection wired to utility, agent/reviewer to primary.

- [ ] **Step 1: Write the failing test** (proves the two tolerant roles use whatever backend they're handed — the plumbing the app relies on)
```python
# append to tests/unit/test_backend_tiering.py
from poseidon.ai.chat import ChatService


def test_chat_service_uses_the_backend_it_is_given() -> None:
    sentinel = object()
    svc = ChatService(AIConfig(), sentinel, _Disp(), None)  # type: ignore[arg-type]
    assert svc._backend is sentinel


class _Disp:
    sources_used: set[str] = set()
    async def dispatch(self, name, args):
        return ("{}", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_backend_tiering.py::test_chat_service_uses_the_backend_it_is_given -q`
Expected: PASS already (ChatService stores `self._backend = backend` today). This is a guard test pinning the plumbing the routing depends on — keep it. (If it errors on construction args, adjust to the real `ChatService.__init__` signature: `(config, backend, dispatcher, db)`.)

- [ ] **Step 3: Wire the routing in `app.py`**

Replace the single-backend build (line ~176):
```python
        self._backend = build_backend(cfg.ai, self.vault.get)
```
with:
```python
        self._backend, self._utility_backend = build_backends(cfg.ai, self.vault.get)
```
Update the import (line ~23) from `build_backend` to also import `build_backends`:
```python
from .ai.backends import ChatBackend, build_backend, build_backends
```
Add the attribute annotation near the other `populated in start()` decls (with `self._backend`): `self._utility_backend: ChatBackend | None = None`.
Point reflection at the utility backend (line ~181): change
`get_backend=lambda: self.agent.backend if self.agent else None,`
to
`get_backend=lambda: self._utility_backend,`.
Point chat at the utility backend (line ~196): change `ChatService(cfg.ai, self._backend, ...)` to `ChatService(cfg.ai, self._utility_backend, ...)`.
Leave the agent (`ClaudeAgent(cfg.ai, self._backend, ...)`) and reviewer (`review_algorithm(self._backend, ...)`) on the primary.
Guard the shutdown double-close (line ~1110):
```python
        if self._backend is not None:
            with contextlib.suppress(Exception):
                await self._backend.aclose()
        if self._utility_backend is not None and self._utility_backend is not self._backend:
            with contextlib.suppress(Exception):
                await self._utility_backend.aclose()
```
(match the existing suppress/aclose style already there; `contextlib` is already imported.)

- [ ] **Step 4: Run the gate**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest -q`
Expected: ruff clean, mypy `Success`, pytest all pass (the full-kernel construction tests exercise the new wiring; a broken route would fail them). ui_verify NOT required (no UI touched).

- [ ] **Step 5: Document + commit**

Under `ai:` in `config/poseidon.example.yaml` add (commented):
```yaml
  # Optional cheap/fast "utility" model for auxiliary roles (operator chat +
  # reflection lessons). Same backend + endpoint as the primary, model swapped;
  # the trading decision always uses the primary. Unset = no tiering.
  #   utility_model: claude-haiku-4-5-20251001   # (Anthropic) or a smaller local model id
```
Add a short matching subsection to `docs/api-configuration.md`. Then:
```bash
git add src/poseidon/app.py config/poseidon.example.yaml docs/api-configuration.md tests/unit/test_backend_tiering.py
git commit -m "feat(app): route chat + reflection to the utility model (tiering)"
```
