"""Starter alpha-factor library. Each Factor maps ONE symbol's point-in-time bars
to a cross-sectional score (higher = more attractive), or None if it can't be
computed from the given window. Pure; built on strategy/indicators.py."""
from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import Bar
from ..strategy.indicators import (
    cumulative_return,
    highest,
    max_drawdown,
    rate_of_change,
    rsi,
    sma,
    stdev_return,
)


@dataclass(frozen=True)
class Factor:
    name: str
    fn: Callable[[list[Bar]], float | None]
    description: str = ""
    min_bars: int = 2


def _closes(bars: list[Bar]) -> list[float]:
    return [float(b.close) for b in bars]


def _vols(bars: list[Bar]) -> list[int]:
    return [b.volume for b in bars]


def _momentum_12_1(bars: list[Bar]) -> float | None:
    c = _closes(bars)
    if len(c) < 253:
        return None
    return c[-22] / c[-253] - 1.0            # 12 months ago -> 1 month ago (skip last 21d)


def _vol_scaled_mom(bars: list[Bar]) -> float | None:
    m = cumulative_return(_closes(bars), 126)
    s = stdev_return(_closes(bars), 20)
    if m is None or not s:
        return None
    return m / s


def _trend_vs_sma50(bars: list[Bar]) -> float | None:
    s = sma(_closes(bars), 50)
    if not s:
        return None
    return _closes(bars)[-1] / s - 1.0


def _drawdown_60(bars: list[Bar]) -> float | None:
    md = max_drawdown(_closes(bars), 60)
    return None if md is None else -abs(md)  # less drawdown preferred


def _volume_ratio(bars: list[Bar]) -> float | None:
    v = _vols(bars)
    if len(v) < 40:
        return None
    base = statistics.fmean(v[-40:-20])
    if base == 0:
        return None
    return statistics.fmean(v[-20:]) / base - 1.0


def _near_52w_high(bars: list[Bar]) -> float | None:
    hi = highest(_closes(bars), 252)
    if not hi:
        return None
    return _closes(bars)[-1] / hi


def _neg(fn: Callable[..., float | None], *a: object) -> Callable[[list[Bar]], float | None]:
    def inner(bars: list[Bar]) -> float | None:
        v = fn(_closes(bars), *a)
        return None if v is None else -v
    return inner


def _pos(fn: Callable[..., float | None], *a: object) -> Callable[[list[Bar]], float | None]:
    def inner(bars: list[Bar]) -> float | None:
        return fn(_closes(bars), *a)
    return inner


def _rsi_reversion(bars: list[Bar]) -> float | None:
    r = rsi(_closes(bars), 14)
    return None if r is None else 50.0 - r    # overbought -> lower score


ALL_FACTORS: list[Factor] = [
    Factor("momentum_12_1", _momentum_12_1, "12m return skipping last month", min_bars=253),
    Factor("momentum_6m", _pos(cumulative_return, 126), "6-month return", min_bars=127),
    Factor("momentum_3m", _pos(cumulative_return, 63), "3-month return", min_bars=64),
    Factor("reversal_1m", _neg(cumulative_return, 21), "negated 1-month return", min_bars=22),
    Factor("reversal_5d", _neg(rate_of_change, 5), "negated 5-day return", min_bars=6),
    Factor("low_vol_20d", _neg(stdev_return, 20), "negated 20d return vol", min_bars=21),
    Factor("rsi_reversion_14", _rsi_reversion, "50 - RSI(14)", min_bars=15),
    Factor("trend_vs_sma50", _trend_vs_sma50, "close/SMA50 - 1", min_bars=51),
    Factor("vol_scaled_mom_6m", _vol_scaled_mom, "6m return / 20d vol", min_bars=127),
    Factor("drawdown_60d", _drawdown_60, "negated |max drawdown| over 60d", min_bars=61),
    Factor("volume_ratio_20", _volume_ratio, "recent/base 20d volume - 1", min_bars=40),
    Factor("near_52w_high", _near_52w_high, "close / 252d high", min_bars=252),
]
