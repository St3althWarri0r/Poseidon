"""Indicator primitives for strategies and workshop algorithms.

The exact functions rotation platforms (Composer, Portfolio Visualizer)
build rules from, so symphonies/models port 1:1. All take a plain list of
closes, oldest first, and return None when there is not enough history —
never a guess.
"""

from __future__ import annotations


def sma(closes: list[float], window: int) -> float | None:
    if window <= 0 or len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def rsi(closes: list[float], window: int = 14) -> float | None:
    """Wilder's RSI (the standard used by Composer/TradingView)."""
    if window <= 0 or len(closes) < window + 1:
        return None
    gains = losses = 0.0
    for i in range(1, window + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / window, losses / window
    for i in range(window + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (window - 1) + max(change, 0.0)) / window
        avg_loss = (avg_loss * (window - 1) + max(-change, 0.0)) / window
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def cumulative_return(closes: list[float], window: int) -> float | None:
    """Return over the last `window` sessions, in PERCENT (Composer units)."""
    if window <= 0 or len(closes) < window + 1 or closes[-window - 1] <= 0:
        return None
    return (closes[-1] / closes[-window - 1] - 1.0) * 100.0


def moving_average_return(closes: list[float], window: int) -> float | None:
    """Mean of the last `window` daily returns, in percent."""
    if window <= 0 or len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0
            for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if not rets:
        return None
    return sum(rets) / len(rets) * 100.0
