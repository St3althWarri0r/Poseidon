"""GET + POST /api/models — the model-selector status + apply endpoints.

See docs/superpowers/specs/2026-07-17-model-selector-design.md (Endpoints 1+2).
GET assembles current backend/model, the curated Claude id list plus whether the
Anthropic key is in the vault, and a best-effort local-endpoint probe + VRAM
hint. It must never raise: the local probe and VRAM detection degrade to
``reachable: false`` / ``null``, and a locked/erroring vault degrades
``key_present`` to ``false`` — a secret is never returned.

POST validates the body (``backend`` in the two allowed values, non-empty
stripped ``model``; bad body → 422), delegates the live swap to
``kernel.apply_ai_config``, maps its precondition/build failures
(``ConfigError``/``VaultError``/``DataError``/``AgentError``) to 422, and echoes
back only the resulting ``ai`` block (``backend/model/base_url/paid`` — never a
secret).
"""

from __future__ import annotations

import httpx
import pytest

from poseidon.ai.backends import CURATED_CLAUDE_MODELS
from poseidon.api.server import build_app
from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig
from poseidon.core.errors import ConfigError
from poseidon.security.vault import Vault


def _kernel(tmp_path) -> ApplicationKernel:
    cfg = AppConfig()
    return ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))


async def _get_models(kernel: ApplicationKernel) -> httpx.Response:
    app = build_app(kernel)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1") as client:
        return await client.get("/api/models")


@pytest.fixture
def stub_probes(monkeypatch):
    """Replace the module-level probe + VRAM helpers on the server so the route
    is exercised without a live LM Studio or a GPU. ``set(reachable, models,
    vram)`` configures the canned results."""
    state: dict[str, object] = {"reachable": False, "models": [], "vram": None}

    async def fake_probe(base_url, *, transport=None):
        return state["reachable"], list(state["models"])

    async def fake_vram():
        return state["vram"]

    monkeypatch.setattr("poseidon.api.server.probe_local_models", fake_probe)
    monkeypatch.setattr("poseidon.api.server.detect_vram", fake_vram)

    def set_(*, reachable, models, vram):
        state.update(reachable=reachable, models=models, vram=vram)

    return set_


async def test_reachable_path_shape(tmp_path, stub_probes) -> None:
    """A reachable local endpoint + detected VRAM produces the full documented
    shape: current backend/model, the curated Claude list, and the live local
    model list with a VRAM hint."""
    stub_probes(reachable=True,
                models=["openai/gpt-oss-20b", "qwen/qwen3-coder-30b"],
                vram={"total_gb": 16.0, "free_gb": 11.2})
    kernel = _kernel(tmp_path)
    resp = await _get_models(kernel)
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_backend"] == "anthropic"
    assert body["current_model"] == kernel.config.ai.model
    assert body["anthropic"]["models"] == list(CURATED_CLAUDE_MODELS)
    assert body["local"] == {
        "reachable": True,
        "models": ["openai/gpt-oss-20b", "qwen/qwen3-coder-30b"],
        "vram": {"total_gb": 16.0, "free_gb": 11.2},
    }


async def test_unreachable_local_degrades(tmp_path, stub_probes) -> None:
    """A down local endpoint degrades to ``reachable: false`` with an empty
    model list — never a 500."""
    stub_probes(reachable=False, models=[], vram=None)
    resp = await _get_models(_kernel(tmp_path))
    assert resp.status_code == 200
    local = resp.json()["local"]
    assert local["reachable"] is False
    assert local["models"] == []


async def test_key_present_reflects_vault(tmp_path, stub_probes) -> None:
    """``key_present`` mirrors whether the configured Anthropic credential name
    is in the vault — true once set, and it never leaks the secret value."""
    stub_probes(reachable=False, models=[], vram=None)
    kernel = _kernel(tmp_path)
    # No key yet.
    resp = await _get_models(kernel)
    assert resp.json()["anthropic"]["key_present"] is False
    # Store the key; it must now report present, without echoing the value.
    kernel.vault.create("test-passphrase")
    kernel.vault.set(kernel.config.ai.api_key_credential, "sk-ant-secret")
    resp = await _get_models(kernel)
    assert resp.json()["anthropic"]["key_present"] is True
    assert "sk-ant-secret" not in resp.text


async def test_locked_vault_key_absent(tmp_path, stub_probes) -> None:
    """A locked vault (names() raises) degrades ``key_present`` to false rather
    than 500ing the read-only endpoint."""
    stub_probes(reachable=False, models=[], vram=None)
    kernel = _kernel(tmp_path)
    assert kernel.vault.unlocked is False
    resp = await _get_models(kernel)
    assert resp.status_code == 200
    assert resp.json()["anthropic"]["key_present"] is False


async def test_vram_null_when_tool_absent(tmp_path, stub_probes) -> None:
    """When no GPU tool is present ``detect_vram`` returns None and the payload
    carries ``vram: null``."""
    stub_probes(reachable=True, models=["m"], vram=None)
    resp = await _get_models(_kernel(tmp_path))
    assert resp.json()["local"]["vram"] is None


# ------------------------------------------------ POST /api/models (apply)


class _RecordingApply:
    """Stand-in for ``kernel.apply_ai_config`` that records the keyword call and
    either returns a canned ``ai`` dict or raises a configured error — so the
    ROUTE (validation, delegation, error-mapping, echo) is exercised in isolation
    from the real live-swap (covered by test_ai_model_selector)."""

    def __init__(self, *, returns=None, raises: Exception | None = None) -> None:
        self.returns = returns
        self.raises = raises
        self.calls: list[dict[str, str]] = []

    async def __call__(self, *, backend: str, model: str) -> dict[str, object]:
        self.calls.append({"backend": backend, "model": model})
        if self.raises is not None:
            raise self.raises
        assert self.returns is not None
        return self.returns


async def _post_models(kernel: ApplicationKernel, body: object) -> httpx.Response:
    app = build_app(kernel)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1") as client:
        return await client.post("/api/models", json=body)


async def test_apply_happy_path_calls_kernel_and_echoes_ai(tmp_path) -> None:
    """A valid body delegates to ``apply_ai_config(backend=…, model=…)`` and
    echoes back exactly the resulting ``ai`` block under ``ok:true`` — no secret,
    no extra keys."""
    kernel = _kernel(tmp_path)
    apply = _RecordingApply(returns={
        "backend": "openai_compatible", "model": "openai/gpt-oss-20b",
        "base_url": "http://localhost:1234/v1", "paid": False,
    })
    kernel.apply_ai_config = apply  # type: ignore[method-assign]
    resp = await _post_models(
        kernel, {"backend": "openai_compatible", "model": "openai/gpt-oss-20b"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ai": {
        "backend": "openai_compatible", "model": "openai/gpt-oss-20b",
        "base_url": "http://localhost:1234/v1", "paid": False,
    }}
    assert apply.calls == [
        {"backend": "openai_compatible", "model": "openai/gpt-oss-20b"}]


async def test_apply_trims_model_before_delegating(tmp_path) -> None:
    """The model id is stripped before it reaches the kernel (a padded but
    non-empty id is accepted, mirroring the broker-name parse)."""
    kernel = _kernel(tmp_path)
    apply = _RecordingApply(returns={
        "backend": "anthropic", "model": "claude-opus-4-8",
        "base_url": None, "paid": True,
    })
    kernel.apply_ai_config = apply  # type: ignore[method-assign]
    resp = await _post_models(
        kernel, {"backend": "anthropic", "model": "  claude-opus-4-8  "})
    assert resp.status_code == 200
    assert apply.calls == [{"backend": "anthropic", "model": "claude-opus-4-8"}]
    assert resp.json()["ai"]["paid"] is True


async def test_apply_missing_key_maps_to_422(tmp_path) -> None:
    """A missing-Anthropic-key precondition (``apply_ai_config`` raises
    ``ConfigError``) surfaces as 422 carrying the guidance message — never a 500,
    and the old backend is untouched (the kernel raised before any swap)."""
    kernel = _kernel(tmp_path)
    apply = _RecordingApply(raises=ConfigError(
        "Set your Anthropic API key in the vault first."))
    kernel.apply_ai_config = apply  # type: ignore[method-assign]
    resp = await _post_models(
        kernel, {"backend": "anthropic", "model": "claude-opus-4-8"})
    assert resp.status_code == 422
    assert "Anthropic API key" in resp.json()["detail"]
    # The kernel WAS consulted (this is a real precondition failure, not a body
    # rejection) — the route delegated before mapping the error.
    assert apply.calls == [{"backend": "anthropic", "model": "claude-opus-4-8"}]


@pytest.mark.parametrize("bad_backend", ["", "gpt-4", "claude", "openai", None])
async def test_apply_bad_backend_422_without_calling_kernel(
        tmp_path, bad_backend) -> None:
    """An unknown/blank/missing backend is rejected 422 by body validation
    BEFORE the kernel is touched — a bad request never reaches the swap."""
    kernel = _kernel(tmp_path)
    apply = _RecordingApply(returns={})
    kernel.apply_ai_config = apply  # type: ignore[method-assign]
    body: dict[str, object] = {"model": "some-model"}
    if bad_backend is not None:
        body["backend"] = bad_backend
    resp = await _post_models(kernel, body)
    assert resp.status_code == 422
    assert apply.calls == []


@pytest.mark.parametrize("bad_model", ["", "   ", None])
async def test_apply_empty_model_422_without_calling_kernel(
        tmp_path, bad_model) -> None:
    """An empty/whitespace/missing model is rejected 422 before delegation — a
    non-empty id is required (custom ids are allowed, blank is not)."""
    kernel = _kernel(tmp_path)
    apply = _RecordingApply(returns={})
    kernel.apply_ai_config = apply  # type: ignore[method-assign]
    body: dict[str, object] = {"backend": "openai_compatible"}
    if bad_model is not None:
        body["model"] = bad_model
    resp = await _post_models(kernel, body)
    assert resp.status_code == 422
    assert apply.calls == []
