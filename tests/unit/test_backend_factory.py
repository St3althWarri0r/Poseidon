from __future__ import annotations

from poseidon.ai.backends import build_backend
from poseidon.ai.backends.anthropic_backend import AnthropicBackend
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.core.config import AIConfig


def test_builds_anthropic_and_resolves_secret() -> None:
    seen: list[str] = []
    b = build_backend(AIConfig(), lambda name: seen.append(name) or "sk-test")
    assert isinstance(b, AnthropicBackend)
    assert seen == ["anthropic_api_key"]


def test_builds_openai_without_secret_lookup() -> None:
    seen: list[str] = []
    cfg = AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1", model="devstral")
    b = build_backend(cfg, lambda name: seen.append(name) or "x")
    assert isinstance(b, OpenAICompatibleBackend)
    assert seen == []  # local backend needs no secret
