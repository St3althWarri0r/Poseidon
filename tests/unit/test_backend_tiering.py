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


# ---- the safety property, asserted on a constructed kernel (spec §5) ----
# The trading agent AND the algorithm reviewer must ALWAYS hold the primary
# backend (the money decision runs on the strong model); only the advisory chat
# and reflection roles move to the utility backend. `_wire_ai` is the construction
# seam that binds each role to its tier — asserting on the wired objects is the
# real invariant (a swap is type-identical, so mypy cannot catch it). Driving the
# full kernel start() would launch the web server, so we exercise the seam directly.

async def test_wire_ai_binds_each_role_to_the_right_tier(tmp_path) -> None:
    from types import SimpleNamespace

    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AppConfig
    from poseidon.security.vault import Vault

    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    # Reflection stores these at construction but never calls them here.
    kernel.db = None  # type: ignore[assignment]
    kernel.router = None  # type: ignore[assignment]
    kernel.audit = SimpleNamespace(append=None)  # type: ignore[assignment]
    disp, chat_disp = object(), object()

    # Tiered: a distinct utility model configured.
    kernel._wire_ai(_cfg(utility_model="small"), disp, chat_disp)  # type: ignore[arg-type]
    assert kernel._utility_backend is not kernel._backend
    assert kernel._backend.model == "big" and kernel._utility_backend.model == "small"
    assert kernel.agent.backend is kernel._backend                      # money path -> primary
    assert kernel.chat._backend is kernel._utility_backend              # chat -> utility
    assert kernel.reflection._get_backend() is kernel._utility_backend  # reflection -> utility

    # No tiering (the default that ships): one shared backend; agent still primary.
    kernel._wire_ai(_cfg(), disp, chat_disp)  # type: ignore[arg-type]
    assert kernel._utility_backend is kernel._backend
    assert kernel.agent.backend is kernel._backend
    assert kernel.chat._backend is kernel._backend
    assert kernel.reflection._get_backend() is kernel._backend


def test_chat_service_uses_the_backend_it_is_given() -> None:
    # Guard the plumbing the routing relies on: ChatService calls whatever backend
    # it is handed, so pointing it at the utility backend actually tiers chat.
    from poseidon.ai.chat import ChatService

    sentinel = object()
    svc = ChatService(AIConfig(), sentinel, object(), None)  # type: ignore[arg-type]
    assert svc._backend is sentinel
