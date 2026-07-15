# tests/unit/test_analysis_config.py
from __future__ import annotations

from poseidon.core.config import AIConfig, AnalysisConfig


def test_analysis_defaults_off() -> None:
    c = AIConfig().analysis
    assert c.enabled is False and c.inject is True
    assert c.debate_rounds == 2 and c.risk_rounds == 1
    assert c.max_injected == 3 and c.max_render_chars == 1200
    assert c.max_symbols_per_sweep == 8 and c.refresh_hours == 24


def test_analysis_bounds() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AnalysisConfig(debate_rounds=0)      # ge=1
    with pytest.raises(ValidationError):
        AnalysisConfig(max_render_chars=10)  # ge=200
