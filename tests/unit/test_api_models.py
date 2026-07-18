"""GET /api/models — the read-only model-selector status endpoint.

See docs/superpowers/specs/2026-07-17-model-selector-design.md (Endpoint 1).
The route assembles current backend/model, the curated Claude id list plus
whether the Anthropic key is in the vault, and a best-effort local-endpoint
probe + VRAM hint. It must never raise: the local probe and VRAM detection
degrade to ``reachable: false`` / ``null``, and a locked/erroring vault
degrades ``key_present`` to ``false`` — a secret is never returned.
"""

from __future__ import annotations

import httpx
import pytest

from poseidon.ai.backends import CURATED_CLAUDE_MODELS
from poseidon.api.server import build_app
from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig
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
