"""Live AI-backend/model swap: ``ApplicationKernel.apply_ai_config``.

Spec §"Kernel apply mechanism". The swap is config-only (no order path, no mode
change, no secret handling): it PROVES the target is usable (anthropic key in the
vault / local endpoint reachable) BEFORE touching anything, rebinds the two frozen
backend refs under ``_cycle_lock`` (so an in-flight decision finishes on its
original brain), persists to the overlay, audits, and closes the displaced
backends. Driven with a fake vault, fake backends (``poseidon.app.build_backends``
patched), and a fake local probe — no network, no real model.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from poseidon.ai.hardware import DEFAULT_LM_STUDIO_URL
from poseidon.app import ApplicationKernel
from poseidon.core.config import AIConfig, AppConfig
from poseidon.core.errors import ConfigError
from poseidon.core.events import Topics
from poseidon.security.vault import Vault

from .backend_fakes import FakeBackend


class ClosableFake(FakeBackend):
    """A FakeBackend that records how many times it was ``aclose``d."""

    def __init__(self, model: str = "fake") -> None:
        super().__init__([])
        self.model = model
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict]] = []

    async def append(self, actor: str, action: str, payload: dict) -> None:
        self.rows.append((actor, action, payload))


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def subscribe(self, topic: str, handler: object) -> None:
        return None

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))


class FakeVault:
    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    def names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str) -> str:
        return "secret"


class _Dispatcher:
    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        return ('{"ok": true}', False)


def _make_kernel(tmp_path, monkeypatch, *, start_ai, build_pairs, vault_names,
                 reachable=True):
    """A minimally-wired kernel: real ctor, then stub the subsystems
    ``apply_ai_config`` touches and patch the two module seams (backend build +
    local probe). ``build_pairs[0]`` feeds the initial ``_wire_ai``; the rest are
    handed out on subsequent ``apply_ai_config`` calls."""
    cfg = AppConfig(config_path=tmp_path / "poseidon.yaml", ai=start_ai)
    kernel = ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))
    kernel.vault = FakeVault(vault_names)  # type: ignore[assignment]
    kernel.db = None  # type: ignore[assignment]
    kernel.router = None  # type: ignore[assignment]
    kernel.audit = FakeAudit()  # type: ignore[assignment]
    kernel.bus = FakeBus()  # type: ignore[assignment]

    pairs = iter(build_pairs)

    def fake_build(c, resolve):
        return next(pairs)

    async def fake_probe(base_url, *, transport=None):
        return reachable, (["m"] if reachable else [])

    monkeypatch.setattr("poseidon.app.build_backends", fake_build)
    monkeypatch.setattr("poseidon.app.probe_local_models", fake_probe)
    kernel._wire_ai(start_ai, _Dispatcher(), _Dispatcher())  # type: ignore[arg-type]
    return kernel


def _overlay_ai(tmp_path):
    return yaml.safe_load((tmp_path / "poseidon.local.yaml").read_text())["ai"]


async def test_same_backend_model_change_rebinds_and_persists(tmp_path, monkeypatch) -> None:
    p0 = ClosableFake("big")
    p1 = ClosableFake("small")
    start = AIConfig(backend="openai_compatible", base_url="http://x/v1", model="big")
    kernel = _make_kernel(tmp_path, monkeypatch, start_ai=start,
                          build_pairs=[(p0, p0), (p1, p1)], vault_names=[])

    result = await kernel.apply_ai_config(backend="openai_compatible", model="small")

    assert kernel._backend is p1                 # reviewer reads _backend fresh -> new
    assert kernel.agent.backend is p1            # agent rebound
    assert kernel.config.ai.model == "small"
    assert kernel.config.ai.backend == "openai_compatible"
    assert p0.closed == 1                         # displaced backend closed once
    assert result == {"backend": "openai_compatible", "model": "small",
                      "base_url": "http://x/v1", "paid": False}
    ai = _overlay_ai(tmp_path)
    assert ai["model"] == "small" and ai["backend"] == "openai_compatible"
    assert "utility_model" not in ai             # same backend -> utility untouched


async def test_anthropic_to_local_rebinds_agent_chat_and_follows(tmp_path, monkeypatch) -> None:
    p0 = ClosableFake("opus")
    u0 = ClosableFake("opus-aux")                # tiered start: distinct utility
    p1 = ClosableFake("local")
    start = AIConfig(utility_model="opus-aux")   # anthropic, tiered, base_url None
    kernel = _make_kernel(tmp_path, monkeypatch, start_ai=start,
                          build_pairs=[(p0, u0), (p1, p1)], vault_names=[])

    result = await kernel.apply_ai_config(backend="openai_compatible", model="local-model")

    assert kernel.agent.backend is p1                        # frozen ref rebound
    assert kernel.chat._backend is p1                        # frozen ref rebound
    assert kernel.reflection._get_backend() is p1            # follows via lambda
    assert kernel.analysis._get_backend() is p1              # follows via lambda
    assert p0.closed == 1 and u0.closed == 1                 # both old backends closed
    assert kernel.config.ai.backend == "openai_compatible"
    assert kernel.config.ai.base_url == DEFAULT_LM_STUDIO_URL
    assert kernel.config.ai.utility_model is None            # cleared on backend change
    ai = _overlay_ai(tmp_path)
    assert ai["base_url"] == DEFAULT_LM_STUDIO_URL
    assert ai["utility_model"] is None                       # explicit null persisted
    assert result["base_url"] == DEFAULT_LM_STUDIO_URL and result["paid"] is False


async def test_local_to_anthropic_key_present_is_paid(tmp_path, monkeypatch) -> None:
    p0 = ClosableFake("local")
    p1 = ClosableFake("opus")
    start = AIConfig(backend="openai_compatible", base_url="http://x/v1", model="local")
    kernel = _make_kernel(tmp_path, monkeypatch, start_ai=start,
                          build_pairs=[(p0, p0), (p1, p1)],
                          vault_names=["anthropic_api_key"])

    result = await kernel.apply_ai_config(backend="anthropic", model="claude-opus-4-8")

    assert result["paid"] is True
    assert kernel.config.ai.backend == "anthropic"
    assert kernel.agent.backend is p1
    assert kernel.config.ai.base_url == "http://x/v1"        # non-local target keeps base_url
    notify = next(p for t, p in kernel.bus.published if t == Topics.NOTIFY)
    assert notify["level"] == "warning"                      # paid switch warns
    row = next(r for r in kernel.audit.rows if r[1] == "ai.backend_changed")
    assert row[2]["paid"] is True


async def test_anthropic_target_without_key_raises_no_swap(tmp_path, monkeypatch) -> None:
    p0 = ClosableFake("local")
    start = AIConfig(backend="openai_compatible", base_url="http://x/v1", model="m")
    # Only one pair: a swap that reached build_backends would StopIteration.
    kernel = _make_kernel(tmp_path, monkeypatch, start_ai=start,
                          build_pairs=[(p0, p0)], vault_names=[])

    with pytest.raises(ConfigError, match="Anthropic API key"):
        await kernel.apply_ai_config(backend="anthropic", model="claude-opus-4-8")

    assert kernel._backend is p0                  # nothing swapped
    assert kernel.agent.backend is p0
    assert kernel.config.ai.backend == "openai_compatible"
    assert p0.closed == 0                          # old backend still live
    assert not (tmp_path / "poseidon.local.yaml").exists()  # no persist


async def test_local_target_unreachable_raises_no_swap(tmp_path, monkeypatch) -> None:
    p0 = ClosableFake("opus")
    start = AIConfig()                             # anthropic default
    kernel = _make_kernel(tmp_path, monkeypatch, start_ai=start,
                          build_pairs=[(p0, p0)], vault_names=["anthropic_api_key"],
                          reachable=False)

    with pytest.raises(ConfigError, match="not reachable"):
        await kernel.apply_ai_config(backend="openai_compatible", model="local")

    assert kernel._backend is p0
    assert kernel.config.ai.backend == "anthropic"
    assert p0.closed == 0
    assert not (tmp_path / "poseidon.local.yaml").exists()


async def test_swap_awaits_a_held_cycle_lock(tmp_path, monkeypatch) -> None:
    p0 = ClosableFake("big")
    p1 = ClosableFake("small")
    start = AIConfig(backend="openai_compatible", base_url="http://x/v1", model="big")
    kernel = _make_kernel(tmp_path, monkeypatch, start_ai=start,
                          build_pairs=[(p0, p0), (p1, p1)], vault_names=[])

    await kernel._cycle_lock.acquire()             # simulate an in-flight cycle
    task = asyncio.create_task(
        kernel.apply_ai_config(backend="openai_compatible", model="small"))
    await asyncio.sleep(0.05)                       # give it time to reach the lock
    assert not task.done()                          # blocked on the running cycle
    assert kernel.config.ai.model == "big"          # swap has NOT landed mid-cycle
    assert kernel._backend is p0

    kernel._cycle_lock.release()                    # cycle finishes -> swap proceeds
    await task
    assert kernel.config.ai.model == "small"
    assert kernel._backend is p1


async def test_audit_and_notify_and_mode_unchanged(tmp_path, monkeypatch) -> None:
    p0 = ClosableFake("opus")
    p1 = ClosableFake("local")
    start = AIConfig()                              # anthropic; mode default RESEARCH
    kernel = _make_kernel(tmp_path, monkeypatch, start_ai=start,
                          build_pairs=[(p0, p0), (p1, p1)], vault_names=[])
    mode_before = kernel.config.mode

    await kernel.apply_ai_config(backend="openai_compatible", model="local")

    actions = [a for _, a, _ in kernel.audit.rows]
    assert "ai.backend_changed" in actions
    assert "mode.changed" not in actions            # switching brains never changes mode
    row = next(r for r in kernel.audit.rows if r[1] == "ai.backend_changed")
    assert row[0] == "human"
    assert row[2] == {"backend": "openai_compatible", "model": "local", "paid": False}
    assert Topics.NOTIFY in [t for t, _ in kernel.bus.published]
    assert kernel.config.mode == mode_before
