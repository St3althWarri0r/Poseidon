from __future__ import annotations

from poseidon.core.errors import (
    AgentError,
    BackendUnreachableError,
    PoseidonError,
)


def test_backend_unreachable_is_agent_error() -> None:
    assert issubclass(BackendUnreachableError, AgentError)


def test_backend_unreachable_is_poseidon_error() -> None:
    assert issubclass(BackendUnreachableError, PoseidonError)


def test_backend_unreachable_is_retryable() -> None:
    assert BackendUnreachableError.retryable is True
    assert BackendUnreachableError("down").retryable is True
