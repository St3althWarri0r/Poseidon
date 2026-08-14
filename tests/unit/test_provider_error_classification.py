"""A 404 is a fact about a symbol, not an outage of a provider.

``DataRouter._route`` skips a provider on a non-retryable error but calls
``record_failure`` on a retryable one — and the penalty box is keyed on the
provider, not on (provider, capability). So classifying a per-symbol 404 as a
retryable outage lets one delisted crypto pair penalise the provider for
*every* capability it serves, including equity QUOTES.

That is not hypothetical: ``data/universe/crypto.txt`` shipped a delisted pair
(DYDX/USD, verified 404 against the public Coinbase API while BTC/MKR/RNDR
return 200), which the crypto screener requests on every refresh.

404/410 are therefore classified non-retryable: the router skips that provider
for this call and moves on, exactly as it does for an auth failure, instead of
recording a failure against it.
"""

from __future__ import annotations

import httpx
import pytest

from poseidon.core.errors import ProviderAuthError, ProviderRateLimitError
from poseidon.data.base import MarketDataProvider


class _Probe(MarketDataProvider):
    name = "probe"
    capabilities = frozenset()


def _decode(status: int) -> None:
    provider = _Probe.__new__(_Probe)
    provider._decode(httpx.Response(status, text="nope"))  # noqa: SLF001


@pytest.mark.parametrize("status", [404, 410])
def test_missing_resource_is_not_retryable(status: int) -> None:
    """A delisted symbol must not penalise the provider."""
    with pytest.raises(Exception) as exc:  # noqa: PT011 - asserting on .retryable
        _decode(status)
    assert getattr(exc.value, "retryable", None) is False, (
        f"HTTP {status} is a permanent fact about the resource; marking it retryable "
        "penalises the provider for every capability it serves"
    )


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_stay_retryable(status: int) -> None:
    """A real outage must still penalise the provider so failover backs off."""
    with pytest.raises(Exception) as exc:  # noqa: PT011
        _decode(status)
    assert getattr(exc.value, "retryable", None) is not False


def test_auth_and_rate_limit_classification_is_unchanged() -> None:
    for status in (401, 403):
        with pytest.raises(ProviderAuthError):
            _decode(status)
    with pytest.raises(ProviderRateLimitError):
        _decode(429)
