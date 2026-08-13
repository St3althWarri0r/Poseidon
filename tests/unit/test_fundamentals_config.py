# tests/unit/test_fundamentals_config.py
"""FundamentalsConfig (r2-wave2 rank 4): OFF by default, bounded knobs, and the
POSEIDON_AI__FUNDAMENTALS__* env-override path through load_config."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from poseidon.core.config import AIConfig, FundamentalsConfig, load_config


def test_fundamentals_defaults_off() -> None:
    c = AIConfig().fundamentals
    assert c.enabled is False              # ship-OFF invariant
    assert c.analyst_context is True       # inert while enabled=False (parent gate)
    assert c.max_statement_periods == 5 and c.max_filings == 10
    assert c.max_insider == 20
    assert c.max_description_chars == 600 and c.digest_max_chars == 900


def test_fundamentals_bounds() -> None:
    with pytest.raises(ValidationError):
        FundamentalsConfig(max_statement_periods=0)   # ge=1
    with pytest.raises(ValidationError):
        FundamentalsConfig(max_statement_periods=13)  # le=12
    with pytest.raises(ValidationError):
        FundamentalsConfig(max_filings=21)            # le=20
    with pytest.raises(ValidationError):
        FundamentalsConfig(max_insider=51)            # le=50
    with pytest.raises(ValidationError):
        FundamentalsConfig(max_description_chars=-1)  # ge=0
    with pytest.raises(ValidationError):
        FundamentalsConfig(digest_max_chars=100)      # ge=200


def test_fundamentals_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        FundamentalsConfig(enable=True)  # type: ignore[call-arg]  # extra=forbid catches typos


def test_env_override_enables(tmp_path, monkeypatch) -> None:
    cfg_file = tmp_path / "poseidon.yaml"
    cfg_file.write_text("mode: research\n")
    monkeypatch.setenv("POSEIDON_AI__FUNDAMENTALS__ENABLED", "true")
    config = load_config(cfg_file)
    assert config.ai.fundamentals.enabled is True
    # untouched siblings keep their defaults
    assert config.ai.fundamentals.max_statement_periods == 5
