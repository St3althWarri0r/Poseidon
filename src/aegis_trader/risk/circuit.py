"""Circuit breakers and trading cooldowns.

The circuit breaker opens when too many errors occur inside a rolling
window (broker rejects, data failures, unexpected exceptions in the
execution path). While open, every order is refused. It re-closes after a
cooldown. Loss-limit halts (daily/weekly/drawdown) are latched separately
by the risk engine and only clear at the next session boundary.

Per-symbol cooldowns prevent rapid-fire re-trading of the same name.
"""

from __future__ import annotations

import time
from collections import deque

import structlog

log = structlog.get_logger(__name__)


class CircuitBreaker:
    def __init__(self, *, error_threshold: int, window_seconds: float,
                 cooldown_seconds: float) -> None:
        self._threshold = error_threshold
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._errors: deque[float] = deque()
        self._open_until = 0.0
        self._manual_reason: str | None = None

    def record_error(self, reason: str = "") -> bool:
        """Record an execution-path error. Returns True if this trip opened
        the breaker."""
        now = time.monotonic()
        self._errors.append(now)
        while self._errors and now - self._errors[0] > self._window:
            self._errors.popleft()
        if len(self._errors) >= self._threshold and not self.is_open:
            self._open_until = now + self._cooldown
            log.error("circuit breaker opened", errors=len(self._errors),
                      window_s=self._window, cooldown_s=self._cooldown, reason=reason)
            return True
        return False

    def force_open(self, reason: str) -> None:
        """Manual/emergency halt; stays open until force_close."""
        self._manual_reason = reason
        log.error("circuit breaker force-opened", reason=reason)

    def force_close(self) -> None:
        self._manual_reason = None
        self._open_until = 0.0
        self._errors.clear()

    @property
    def is_open(self) -> bool:
        return self._manual_reason is not None or time.monotonic() < self._open_until

    @property
    def reason(self) -> str | None:
        if self._manual_reason:
            return self._manual_reason
        if time.monotonic() < self._open_until:
            remaining = int(self._open_until - time.monotonic())
            return f"error-rate trip, {remaining}s of cooldown remaining"
        return None


class TradeCooldowns:
    def __init__(self, *, per_symbol_seconds: float) -> None:
        self._per_symbol = per_symbol_seconds
        self._last_trade: dict[str, float] = {}

    def record_trade(self, symbol: str) -> None:
        self._last_trade[symbol.upper()] = time.monotonic()

    def remaining(self, symbol: str) -> float:
        last = self._last_trade.get(symbol.upper())
        if last is None:
            return 0.0
        return max(0.0, self._per_symbol - (time.monotonic() - last))
