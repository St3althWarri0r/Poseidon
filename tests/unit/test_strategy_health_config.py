# tests/unit/test_strategy_health_config.py
from __future__ import annotations

from poseidon.core.config import AppConfig, StrategyHealthConfig


def test_defaults() -> None:
    c = AppConfig().strategy_health
    assert isinstance(c, StrategyHealthConfig)
    assert c.enabled is True and c.auto_retire is False       # advisory by default
    assert c.window_trades == 20 and c.min_trades == 8
    assert c.baseline_min_trades == 20 and c.decay_t == 2.0
    assert c.decay_streak == 2 and c.retire_streak == 4 and c.recover_streak == 2
