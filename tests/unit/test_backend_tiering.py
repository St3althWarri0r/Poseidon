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
