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


def ema(closes: list[float], window: int) -> float | None:
    """Exponential moving average (Composer: exponential-moving-average-price)."""
    if window <= 0 or len(closes) < window:
        return None
    k = 2.0 / (window + 1)
    value = sum(closes[:window]) / window
    for price in closes[window:]:
        value = price * k + value * (1 - k)
    return value


def stdev_price(closes: list[float], window: int) -> float | None:
    """Population stdev of price (Composer: standard-deviation-price)."""
    if window <= 1 or len(closes) < window:
        return None
    sample = closes[-window:]
    mean = sum(sample) / window
    return float((sum((p - mean) ** 2 for p in sample) / window) ** 0.5)


def stdev_return(closes: list[float], window: int) -> float | None:
    """Population stdev of daily returns in percent (Composer:
    standard-deviation-return)."""
    if window <= 1 or len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0
            for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    return float((sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5) * 100.0


def max_drawdown(closes: list[float], window: int) -> float | None:
    """Deepest peak-to-trough decline over the window, in percent (positive
    number; Composer: max-drawdown)."""
    if window <= 0 or len(closes) < window:
        return None
    peak, worst = float("-inf"), 0.0
    for price in closes[-window:]:
        peak = max(peak, price)
        if peak > 0:
            worst = max(worst, (peak - price) / peak)
    return worst * 100.0


def rate_of_change(closes: list[float], window: int) -> float | None:
    """Alias of cumulative_return (classic ROC, percent)."""
    return cumulative_return(closes, window)


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[float, float, float] | None:
    """(macd_line, signal_line, histogram). None without enough history."""
    if len(closes) < slow + signal:
        return None

    def ema_series(window: int) -> list[float]:
        k = 2.0 / (window + 1)
        out = [sum(closes[:window]) / window]
        for price in closes[window:]:
            out.append(price * k + out[-1] * (1 - k))
        return out

    fast_s, slow_s = ema_series(fast), ema_series(slow)
    line = [f - s for f, s in zip(fast_s[-len(slow_s):], slow_s, strict=True)]
    if len(line) < signal:
        return None
    k = 2.0 / (signal + 1)
    sig = sum(line[:signal]) / signal
    for value in line[signal:]:
        sig = value * k + sig * (1 - k)
    return line[-1], sig, line[-1] - sig


def bollinger(closes: list[float], window: int = 20,
              num_std: float = 2.0) -> tuple[float, float, float, float] | None:
    """(upper, middle, lower, percent_b). percent_b: 0 at lower, 1 at upper."""
    mid = sma(closes, window)
    std = stdev_price(closes, window)
    if mid is None or std is None:
        return None
    upper, lower = mid + num_std * std, mid - num_std * std
    pct_b = (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5
    return upper, mid, lower, pct_b


def highest(closes: list[float], window: int) -> float | None:
    return max(closes[-window:]) if 0 < window <= len(closes) else None


def lowest(closes: list[float], window: int) -> float | None:
    return min(closes[-window:]) if 0 < window <= len(closes) else None


def stochastic(highs: list[float], lows: list[float], closes: list[float],
               k_window: int = 14, d_window: int = 3) -> tuple[float, float] | None:
    """Fast %K and its SMA %D, 0..100."""
    n = min(len(highs), len(lows), len(closes))
    if n < k_window + d_window - 1:
        return None
    ks = []
    for end in range(n - d_window + 1, n + 1):
        hi = max(highs[end - k_window:end])
        lo = min(lows[end - k_window:end])
        ks.append(100.0 * (closes[end - 1] - lo) / (hi - lo) if hi > lo else 50.0)
    return ks[-1], sum(ks) / len(ks)


def atr(highs: list[float], lows: list[float], closes: list[float],
        window: int = 14) -> float | None:
    """Wilder's average true range."""
    n = min(len(highs), len(lows), len(closes))
    if n < window + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    value = sum(trs[:window]) / window
    for tr in trs[window:]:
        value = (value * (window - 1) + tr) / window
    return value


def adx(highs: list[float], lows: list[float], closes: list[float],
        window: int = 14) -> float | None:
    """Wilder's average directional index, 0..100."""
    n = min(len(highs), len(lows), len(closes))
    if n < 2 * window + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    tr_s, pdm_s, mdm_s = sum(trs[:window]), sum(plus_dm[:window]), sum(minus_dm[:window])
    dxs = []
    for i in range(window, len(trs)):
        tr_s = tr_s - tr_s / window + trs[i]
        pdm_s = pdm_s - pdm_s / window + plus_dm[i]
        mdm_s = mdm_s - mdm_s / window + minus_dm[i]
        if tr_s <= 0:
            continue
        pdi, mdi = 100.0 * pdm_s / tr_s, 100.0 * mdm_s / tr_s
        dxs.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0)
    if len(dxs) < window:
        return None
    value = sum(dxs[:window]) / window
    for dx in dxs[window:]:
        value = (value * (window - 1) + dx) / window
    return value


def obv(closes: list[float], volumes: list[int]) -> float | None:
    """On-balance volume over the whole provided series."""
    n = min(len(closes), len(volumes))
    if n < 2:
        return None
    total = 0.0
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            total += volumes[i]
        elif closes[i] < closes[i - 1]:
            total -= volumes[i]
    return total
