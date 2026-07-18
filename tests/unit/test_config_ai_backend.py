from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from poseidon.app import ApplicationKernel
from poseidon.core.config import AIConfig, AppConfig, load_config, local_overlay_path
from poseidon.security.vault import Vault


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


# ---------------------------------------------------- _write_ai_overlay (task 1)


def _kernel(tmp_path, ai: AIConfig | None = None) -> ApplicationKernel:
    cfg = AppConfig(ai=ai or AIConfig())
    cfg.config_path = tmp_path / "poseidon.yaml"
    return ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))


def test_write_ai_overlay_roundtrips_to_config(tmp_path) -> None:
    """The overlay a live switch writes must load back (through
    apply_local_overlay + load_config) to exactly the chosen backend/model."""
    kernel = _kernel(tmp_path)  # base defaults: anthropic / claude-opus-4-8
    target = AIConfig(
        backend="openai_compatible",
        base_url="http://localhost:1234/v1",
        model="openai/gpt-oss-20b",
    )
    kernel._write_ai_overlay(target)

    loaded = load_config(kernel.config.config_path)
    assert loaded.ai.backend == "openai_compatible"
    assert loaded.ai.model == "openai/gpt-oss-20b"
    assert loaded.ai.base_url == "http://localhost:1234/v1"


def test_write_ai_overlay_preserves_existing_broker_overlay(tmp_path) -> None:
    """Writing the ai sub-block must not clobber a broker choice already
    persisted in the same overlay file."""
    kernel = _kernel(tmp_path)
    overlay_file = local_overlay_path(kernel.config.config_path)
    overlay_file.parent.mkdir(parents=True, exist_ok=True)
    overlay_file.write_text(
        yaml.safe_dump({"brokers": [{"name": "alpaca", "primary": True, "paper": True}]}),
        encoding="utf-8",
    )

    kernel._write_ai_overlay(
        AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1", model="m")
    )

    data = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
    assert data["ai"]["backend"] == "openai_compatible"
    assert data["brokers"][0]["name"] == "alpaca"
    assert data["brokers"][0]["primary"] is True


def test_write_ai_overlay_writes_no_secret(tmp_path) -> None:
    """Only backend/model/base_url are persisted — never a credential value and
    never even the api_key_credential name key."""
    kernel = _kernel(tmp_path)
    kernel.vault.create("test-passphrase")
    kernel.vault.set("anthropic_api_key", "sk-SECRET-VALUE-123")

    kernel._write_ai_overlay(AIConfig(model="claude-haiku-4-5-20251001"))

    text = local_overlay_path(kernel.config.config_path).read_text(encoding="utf-8")
    assert "sk-SECRET-VALUE-123" not in text
    assert "api_key" not in text
    data = yaml.safe_load(text)
    assert set(data["ai"]) == {"backend", "model", "base_url"}


def test_write_ai_overlay_emits_null_utility_on_backend_change(tmp_path) -> None:
    """A backend change clears the (now cross-backend-stale) utility model, so
    the overlay writes an explicit null to override the base value."""
    kernel = _kernel(tmp_path)
    kernel._write_ai_overlay(
        AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1", model="m"),
        clear_utility=True,
    )
    data = yaml.safe_load(local_overlay_path(kernel.config.config_path).read_text(encoding="utf-8"))
    assert "utility_model" in data["ai"]
    assert data["ai"]["utility_model"] is None


def test_write_ai_overlay_omits_utility_without_backend_change(tmp_path) -> None:
    """A same-backend model change leaves utility_model out of the overlay so a
    base ai.utility_model survives the startup deep-merge."""
    kernel = _kernel(tmp_path)
    kernel._write_ai_overlay(AIConfig(model="claude-haiku-4-5-20251001"))
    data = yaml.safe_load(local_overlay_path(kernel.config.config_path).read_text(encoding="utf-8"))
    assert "utility_model" not in data["ai"]


def test_write_ai_overlay_null_utility_overrides_base_utility(tmp_path) -> None:
    """End-to-end: the explicit null must win over a base ai.utility_model set
    in poseidon.yaml when a backend change cleared it."""
    kernel = _kernel(tmp_path)
    kernel.config.config_path.write_text(
        yaml.safe_dump({"ai": {"utility_model": "claude-haiku-4-5-20251001"}}),
        encoding="utf-8",
    )
    kernel._write_ai_overlay(
        AIConfig(
            backend="openai_compatible",
            base_url="http://localhost:1234/v1",
            model="m",
            utility_model=None,
        ),
        clear_utility=True,
    )
    loaded = load_config(kernel.config.config_path)
    assert loaded.ai.utility_model is None
