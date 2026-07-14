# Deep/Quick Model Tiering — Design Spec

**Date:** 2026-07-14
**Status:** Approved (design), pending implementation plan
**Target:** Poseidon v2.10.0 candidate
**Origin:** Sub-project #2 of the cross-pollination program ([[poseidon-crosspollination-program]]),
from TradingAgents' deep/quick model tiering (paper §4.3). Small, isolated.

## 1. Goal

Let Poseidon run its **auxiliary** AI roles on a cheaper/faster "utility" model
while the **trading decision** stays on the strong primary model. This is opt-in
infrastructure: a cost/latency win on the Anthropic path (Haiku for aux, Opus/
Fable for the decision), and the seam that makes sub-project #3's multi-analyst
fan-out affordable. On a local-only setup (primary already free Devstral) it is
near-zero value and stays off by default.

## 2. Invariants (the one safety property + the guardrails)

- **The trading decision is ALWAYS on the primary model.** `ClaudeAgent`
  (`run_cycle` → `submit_decision`) must never be handed the utility backend.
  This is the whole safety surface; everything else is advisory.
- **Backward-compatible.** No `utility_model` configured ⇒ no second backend ⇒
  every role uses the primary exactly as today (byte-identical behavior).
- **No touch to the decision / risk / order / audit path.** This change only
  swaps which `ChatBackend` instance the chat and reflection roles call.

## 3. Design

Today `app.py` builds one `self._backend = build_backend(cfg.ai, vault.get)` and
hands it to all four AI roles (agent, chat, reviewer, reflection). Tiering adds a
second backend and re-routes the two tolerant auxiliary roles to it.

### 3.1 Config — one field
`AIConfig.utility_model: str | None = None` (core/config.py). When set, the
utility backend is the **same backend and endpoint** as the primary with only the
model swapped — which is exactly how both target setups work: Anthropic
Opus→Haiku (same account/credential), and LM Studio Devstral→a smaller model at
the same `localhost:1234` (one endpoint serves many models). Cross-backend
utility (e.g. local primary + Anthropic utility) and a separate utility
temperature are out of scope (YAGNI).

### 3.2 Build — `app.py`
After building the primary `self._backend`:
```
self._utility_backend = (
    self._backend if not cfg.ai.utility_model
    else build_backend(cfg.ai.model_copy(update={"model": cfg.ai.utility_model}), self.vault.get)
)
```
So with no `utility_model`, `self._utility_backend is self._backend` (same object).

### 3.3 Route
| Role | Backend | Why |
|---|---|---|
| Trading agent (`run_cycle`) | **primary** | the money decision — always strong |
| Algorithm reviewer | **primary** | forces a tool (`tool_choice=required`); weak models drive forced calls unreliably; rare + reasoning-heavy |
| Operator chat | **utility** | `DATA_TOOLS`, unforced; tolerant; per-message |
| Reflection loop | **utility** | `tools=[]`; advisory; per close |

Concretely: `ChatService(cfg.ai, self._utility_backend, …)`; the reflection
service's `get_backend` lambda returns `self._utility_backend` (was
`self.agent.backend`); `agent` and `review_algorithm(self._backend, …)` unchanged.

### 3.4 Shutdown
Close the utility backend on shutdown **only when it is a distinct instance**
(`if self._utility_backend is not None and self._utility_backend is not self._backend: await self._utility_backend.aclose()`), so a no-tiering run doesn't double-close the shared backend.

## 4. Error handling

Nothing new. A utility-backend failure surfaces exactly like a primary-backend
failure does today in that role (chat catches `AgentError`; reflection is
fail-open). The utility model producing a weaker chat reply or lesson is the
accepted trade-off, bounded to advisory roles.

## 5. Testing

- **The safety property:** the constructed `agent` holds the primary backend and
  (when `utility_model` is set to a distinct model) `chat`/reflection hold the
  utility backend — asserted directly on the wired kernel.
- Config: `utility_model` parses; default `None`.
- `utility_model=None` ⇒ `_utility_backend is _backend` (no second backend,
  behavior unchanged); set ⇒ a distinct instance whose `.model` is the utility model.
- Shutdown closes the utility backend once when distinct, and does not
  double-close when shared.
- Full gate (ruff / mypy --strict / pytest). No multi-agent review — the surface
  is one property and never touches the decision/risk path.

## 6. Scope / YAGNI

- **No intra-loop tiering** (cheap model for the agent's data-gathering
  iterations, strong for the decision) — the ReAct loop isn't cleanly separable;
  fragile and risks decision quality. Possible future work.
- **No cross-backend utility, no separate utility temperature, no per-role
  knobs.** One field, two tolerant roles.
- **Honest framing for the release notes:** on the user's current local-Devstral
  setup this is opt-in infrastructure with near-zero immediate value (a smaller
  local model, still $0); do not sell it as "now cheaper." The payoff is the
  Anthropic path and enabling #3.
