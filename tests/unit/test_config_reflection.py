from __future__ import annotations

import pytest
from pydantic import ValidationError

from poseidon.core.config import AIConfig, ReflectionConfig


def test_defaults_are_closed_loop() -> None:
    c = AIConfig().reflection
    assert c.enabled is True and c.inject is True
    assert c.max_injected == 8 and c.per_symbol == 2 and c.global_n == 3
    assert c.lookback_days == 120


def test_reflection_block_parses_and_overrides() -> None:
    c = AIConfig(reflection={"inject": False, "max_injected": 4}).reflection
    assert c.inject is False and c.max_injected == 4 and c.enabled is True


def test_negative_caps_rejected() -> None:
    with pytest.raises(ValidationError):
        ReflectionConfig(max_injected=-1)
    with pytest.raises(ValidationError):
        ReflectionConfig(lookback_days=0)


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ReflectionConfig(bogus=1)  # type: ignore[call-arg]
