from __future__ import annotations

import pytest
from pydantic import ValidationError

from poseidon.core.config import (
    AIConfig,
    BehaviorConfig,
    OutcomeResolutionConfig,
    ReflectionConfig,
    RiskConfig,
)


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


# ---- outcome-resolution + behavior blocks (invariant 6: OFF by default) -----


def test_outcomes_and_behavior_default_off() -> None:
    c = AIConfig().reflection
    assert c.outcomes.enabled is False
    assert c.behavior.enabled is False


def test_outcomes_defaults() -> None:
    o = AIConfig().reflection.outcomes
    assert o.horizon_trading_days == 5 and o.max_decisions_per_sweep == 25
    assert o.max_lessons_per_sweep == 3 and o.min_abs_alpha == 0.02
    assert o.max_age_days == 45


def test_behavior_defaults() -> None:
    b = AIConfig().reflection.behavior
    assert b.window_days == 90 and b.min_trades == 10 and b.runup_days == 5
    assert b.runup_threshold == 0.05 and b.reentry_days == 3
    assert b.max_bar_symbols == 20


def test_nested_overrides_parse() -> None:
    c = AIConfig(reflection={
        "outcomes": {"enabled": True, "horizon_trading_days": 10},
        "behavior": {"enabled": True, "min_trades": 4},
    }).reflection
    assert c.outcomes.enabled is True and c.outcomes.horizon_trading_days == 10
    assert c.behavior.enabled is True and c.behavior.min_trades == 4


def test_outcome_and_behavior_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        OutcomeResolutionConfig(horizon_trading_days=0)
    with pytest.raises(ValidationError):
        BehaviorConfig(min_trades=1)
    with pytest.raises(ValidationError):
        BehaviorConfig(runup_threshold=0)


def test_max_age_must_cover_weekend_stretched_horizon() -> None:
    # N trading bars span >= N calendar days (weekends/holidays stretch them
    # to ~2N): a max_age below 2*horizon would age every decision out before
    # its forward bars can even exist.
    with pytest.raises(ValidationError):
        OutcomeResolutionConfig(horizon_trading_days=30, max_age_days=45)
    ok = OutcomeResolutionConfig(horizon_trading_days=30, max_age_days=60)  # boundary
    assert ok.max_age_days == 60


def test_unknown_fields_rejected_on_new_blocks() -> None:
    with pytest.raises(ValidationError):
        OutcomeResolutionConfig(bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BehaviorConfig(bogus=1)  # type: ignore[call-arg]


def test_crypto_benchmark_symbol_default() -> None:
    assert RiskConfig().crypto_benchmark_symbol == "BTC/USD"
