# tests/unit/test_decay_advance.py
from __future__ import annotations

from poseidon.analytics.decay import HealthState, Signal, advance
from poseidon.core.config import StrategyHealthConfig


def _cfg(**kw) -> StrategyHealthConfig:
    return StrategyHealthConfig(**kw)


def test_single_dying_only_reaches_watch() -> None:
    cfg = _cfg(decay_streak=2, retire_streak=4)
    state, d, r = advance(HealthState.HEALTHY, 0, 0, Signal.DYING, cfg)
    assert state is HealthState.WATCH and d == 1        # NOT decaying on one sweep


def test_streak_escalates_to_decaying_then_retire() -> None:
    cfg = _cfg(decay_streak=2, retire_streak=4)
    state, d, r = HealthState.HEALTHY, 0, 0
    seen = []
    for _ in range(5):
        state, d, r = advance(state, d, r, Signal.DYING, cfg)
        seen.append(state)
    assert seen[0] is HealthState.WATCH                 # streak 1
    assert seen[1] is HealthState.DECAYING              # streak 2 == decay_streak
    assert seen[3] is HealthState.RETIRE_RECOMMENDED    # streak 4 == retire_streak


def test_softening_caps_at_watch_and_resets_decline() -> None:
    cfg = _cfg()
    state, d, r = advance(HealthState.HEALTHY, 3, 0, Signal.SOFTENING, cfg)
    assert state is HealthState.WATCH and d == 0        # not dying -> no escalation


def test_insufficient_holds_state_and_counters() -> None:
    cfg = _cfg()
    assert advance(HealthState.DECAYING, 3, 0, Signal.INSUFFICIENT, cfg) == (
        HealthState.DECAYING, 3, 0)


def test_ok_recovers_one_rung_after_recover_streak() -> None:
    cfg = _cfg(recover_streak=2)
    state, d, r = advance(HealthState.DECAYING, 0, 0, Signal.OK, cfg)
    assert state is HealthState.DECAYING and r == 1     # not yet
    state, d, r = advance(state, d, r, Signal.OK, cfg)
    assert state is HealthState.WATCH and r == 0        # stepped down one rung
