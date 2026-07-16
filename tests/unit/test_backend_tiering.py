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


async def test_review_algorithm_hands_the_reviewer_the_primary_backend(
    tmp_path, monkeypatch
) -> None:
    # The other half of invariant 3: the algorithm reviewer vets code that can
    # become a live strategy, so it must ALWAYS run on the primary backend.
    # kernel.review_algorithm is the seam (app.py passes self._backend to
    # ai.reviewer.review_algorithm); assert OBJECT IDENTITY of what actually
    # crosses it — under tiering the utility backend is a distinct live object,
    # so a "save tokens on reviews" regression to self._utility_backend fails here.
    from types import SimpleNamespace

    import poseidon.ai.reviewer as reviewer_mod
    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AppConfig
    from poseidon.security.vault import Vault

    captured: list[object] = []

    async def _fake_review(backend, *, source, instructions="", max_tokens=8000):  # type: ignore[no-untyped-def]
        captured.append(backend)
        return {"convertible": False, "suggested_name": None,
                "usage": {"input_tokens": 0, "output_tokens": 0, "api_calls": 1}}

    # app.py imports the reviewer lazily inside the method, so patching the
    # source module intercepts the real call path, not a stale reference.
    monkeypatch.setattr(reviewer_mod, "review_algorithm", _fake_review)

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    # review_algorithm meters usage and audits; the seam under test only needs
    # these calls to succeed, not to persist.
    kernel.db = SimpleNamespace(execute=_noop)  # type: ignore[assignment]
    kernel.router = None  # type: ignore[assignment]
    kernel.audit = SimpleNamespace(append=_noop)  # type: ignore[assignment]
    disp, chat_disp = object(), object()

    # Tiered: the reviewer must receive the primary (money) backend, never utility.
    kernel._wire_ai(_cfg(utility_model="small"), disp, chat_disp)  # type: ignore[arg-type]
    await kernel.review_algorithm(source="def f(): pass")
    assert captured[-1] is kernel._backend
    assert captured[-1] is not kernel._utility_backend

    # No tiering: one shared backend; the reviewer still holds the primary object.
    kernel._wire_ai(_cfg(), disp, chat_disp)  # type: ignore[arg-type]
    await kernel.review_algorithm(source="def f(): pass")
    assert captured[-1] is kernel._backend


def test_chat_service_uses_the_backend_it_is_given() -> None:
    # Guard the plumbing the routing relies on: ChatService calls whatever backend
    # it is handed, so pointing it at the utility backend actually tiers chat.
    from poseidon.ai.chat import ChatService

    sentinel = object()
    svc = ChatService(AIConfig(), sentinel, object(), None)  # type: ignore[arg-type]
    assert svc._backend is sentinel
