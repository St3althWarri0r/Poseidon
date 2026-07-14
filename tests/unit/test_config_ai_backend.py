from __future__ import annotations

import pytest
from pydantic import ValidationError

from poseidon.core.config import AIConfig


def test_defaults_are_anthropic() -> None:
    c = AIConfig()
    assert c.backend == "anthropic"
    assert c.base_url is None


def test_openai_compatible_requires_base_url() -> None:
    with pytest.raises(ValidationError):
        AIConfig(backend="openai_compatible")


def test_openai_compatible_with_base_url_ok() -> None:
    c = AIConfig(
        backend="openai_compatible",
        base_url="http://localhost:1234/v1",
        model="devstral-small-2-24b-instruct-2512",
    )
    assert c.base_url is not None and c.base_url.endswith("/v1")
    assert 0.0 <= c.temperature <= 2.0


def test_anthropic_requires_api_key_credential() -> None:
    with pytest.raises(ValidationError):
        AIConfig(backend="anthropic", api_key_credential="")
