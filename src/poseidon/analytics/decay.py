"""Strategy-decay assessment + lifecycle state machine. Pure — no I/O. Only a
genuinely-unprofitable edge (DYING) escalates toward retirement; a lower-but-still-
positive edge is SOFTENING (normalization, not death) and caps at WATCH."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import StrEnum

from ..core.config import StrategyHealthConfig
from .performance import RoundTrip


class HealthState(StrEnum):
    HEALTHY = "healthy"
    WATCH = "watch"
    DECAYING = "decaying"
    RETIRE_RECOMMENDED = "retire_recommended"


class Signal(StrEnum):
    INSUFFICIENT = "insufficient"
    OK = "ok"
    SOFTENING = "softening"
    DYING = "dying"


@dataclass(frozen=True)
class Assessment:
    signal: Signal
    window_return: float
    baseline_return: float
    t0: float
    trades: int
    win_rate: float


def assess(trips: list[RoundTrip], cfg: StrategyHealthConfig) -> Assessment:
    ordered = sorted(trips, key=lambda t: t.exited_at)
    window = ordered[-cfg.window_trades:]
    baseline = ordered[:-cfg.window_trades]
    n = len(window)
    wr = [t.return_pct for t in window]
    win_mean = statistics.fmean(wr) if wr else 0.0
    win_rate = (sum(1 for t in window if t.pnl > 0) / n) if n else 0.0
    base_mean = statistics.fmean([t.return_pct for t in baseline]) if baseline else 0.0
    if n < cfg.min_trades or len(baseline) < cfg.baseline_min_trades:
        return Assessment(Signal.INSUFFICIENT, win_mean, base_mean, 0.0, n, win_rate)
    win_std = statistics.stdev(wr) if n >= 2 else 0.0
    if win_std == 0.0:                       # degenerate all-equal window: no t-stat
        sig = (Signal.DYING if win_mean < 0
               else Signal.SOFTENING if win_mean < base_mean else Signal.OK)
        return Assessment(sig, win_mean, base_mean, 0.0, n, win_rate)
    se = win_std / math.sqrt(n)
    t0 = win_mean / se                       # one-sample t-test vs 0
    if t0 <= -cfg.decay_t:
        return Assessment(Signal.DYING, win_mean, base_mean, t0, n, win_rate)
    if win_mean > 0 and win_mean < base_mean - cfg.decay_t * se:
        return Assessment(Signal.SOFTENING, win_mean, base_mean, t0, n, win_rate)
    return Assessment(Signal.OK, win_mean, base_mean, t0, n, win_rate)


_LADDER = [HealthState.HEALTHY, HealthState.WATCH, HealthState.DECAYING,
           HealthState.RETIRE_RECOMMENDED]


def _down_one(state: HealthState) -> HealthState:
    return _LADDER[max(0, _LADDER.index(state) - 1)]


def is_downgrade(old: HealthState, new: HealthState) -> bool:
    """True iff `new` is strictly worse than `old` on the health ladder. A
    hysteresis recovery (e.g. retire_recommended -> decaying) moves to a state
    that is still a member of the "bad" states but is an IMPROVEMENT, not a
    downgrade — callers deciding whether to warn must check this, not just
    membership in the bad-state set."""
    return _LADDER.index(new) > _LADDER.index(old)


def advance(state: HealthState, decline_streak: int, recover_streak: int,
            signal: Signal, cfg: StrategyHealthConfig) -> tuple[HealthState, int, int]:
    """Hysteresis transition. Only DYING escalates toward retirement."""
    if signal is Signal.DYING:
        d = decline_streak + 1
        if state in (HealthState.HEALTHY, HealthState.WATCH):
            state = HealthState.DECAYING if d >= cfg.decay_streak else HealthState.WATCH
        elif state is HealthState.DECAYING and d >= cfg.retire_streak:
            state = HealthState.RETIRE_RECOMMENDED
        return state, d, 0
    if signal is Signal.SOFTENING:
        if state is HealthState.HEALTHY:
            state = HealthState.WATCH
        return state, 0, 0                    # not dying: reset decline, no recovery
    if signal is Signal.OK:
        r = recover_streak + 1
        if state is not HealthState.HEALTHY and r >= cfg.recover_streak:
            return _down_one(state), 0, 0
        return state, 0, r
    return state, decline_streak, recover_streak     # INSUFFICIENT holds everything
