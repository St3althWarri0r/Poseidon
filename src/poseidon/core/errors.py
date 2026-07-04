"""Exception hierarchy.

Every subsystem raises subclasses of :class:`PoseidonError` so the kernel and
watchdog can distinguish recoverable operational failures (retry / failover)
from configuration or safety failures (halt and alert).
"""

from __future__ import annotations


class PoseidonError(Exception):
    """Base class for all platform errors."""

    retryable: bool = False


class ConfigError(PoseidonError):
    """Invalid or missing configuration. Never retryable."""


class VaultError(PoseidonError):
    """Credential vault failures (wrong passphrase, corrupt store)."""


class VaultLockedError(VaultError):
    """The vault has not been unlocked this session."""


# -- Data layer -------------------------------------------------------------


class DataError(PoseidonError):
    retryable = True


class ProviderError(DataError):
    """A single provider failed; the router will fail over."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


class ProviderAuthError(ProviderError):
    def __init__(self, provider: str, message: str = "authentication failed") -> None:
        super().__init__(provider, message, retryable=False)


class ProviderRateLimitError(ProviderError):
    def __init__(self, provider: str, retry_after: float | None = None) -> None:
        super().__init__(provider, "rate limited", retryable=True)
        self.retry_after = retry_after


class AllProvidersFailedError(DataError):
    """Every configured provider failed for a request. The AI must not trade."""

    retryable = True


class StaleDataError(DataError):
    """Data was retrieved but is older than the staleness threshold."""

    retryable = True


class DataUnavailableError(DataError):
    """Required data simply is not obtainable right now. Trading must pause."""

    retryable = True


# -- Brokers ----------------------------------------------------------------


class BrokerError(PoseidonError):
    retryable = True

    def __init__(self, broker: str, message: str, *, retryable: bool = True,
                 ambiguous: bool = False) -> None:
        super().__init__(f"[{broker}] {message}")
        self.broker = broker
        self.retryable = retryable
        # Ambiguous: the order's outcome is UNKNOWN (e.g. a submit that timed
        # out after the request was sent to a broker with no idempotency key).
        # Such an order must never be auto-resubmitted — it is marked ERROR and
        # reconciled against the broker's open orders at startup.
        self.ambiguous = ambiguous


class BrokerAuthError(BrokerError):
    def __init__(self, broker: str, message: str = "authentication failed") -> None:
        super().__init__(broker, message, retryable=False)


class BrokerNotSupportedError(BrokerError):
    """Raised by documented stub plugins for brokers without an official API."""

    def __init__(self, broker: str, message: str) -> None:
        super().__init__(broker, message, retryable=False)


class OrderRejectedError(BrokerError):
    def __init__(self, broker: str, message: str) -> None:
        super().__init__(broker, message, retryable=False)


# -- Risk / execution --------------------------------------------------------


class RiskViolation(PoseidonError):  # noqa: N818 — domain term
    """An order or decision violated a risk rule. Never retryable."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"risk rule '{rule}': {message}")
        self.rule = rule


class CircuitBreakerOpen(PoseidonError):  # noqa: N818 — domain term
    """Trading is halted by a circuit breaker or cooldown."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"circuit breaker open: {reason}")
        self.reason = reason


class ExecutionError(PoseidonError):
    retryable = True


class DuplicateOrderError(ExecutionError):
    retryable = False


# -- AI ----------------------------------------------------------------------


class AgentError(PoseidonError):
    retryable = True


class AgentRefusedError(AgentError):
    """The model declined the request; the cycle is skipped, never faked."""

    retryable = False
