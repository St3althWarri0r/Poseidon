"""F011 regression: unparseable provider data must fail over, not escape the router.

Part-1 hardening sweep (commit 3a10e42), src/poseidon/data/router.py. A provider
whose quote() raises a non-PoseidonError (e.g. finnhub's `Decimal(str("N/A"))`
raising decimal.InvalidOperation) previously escaped DataRouter._route entirely,
skipping failover and the penalty box. This pins the new
`except (ArithmeticError, TypeError, ValueError)` failover branch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poseidon.core.clock import FreshnessPolicy
from poseidon.core.models import Quote
from poseidon.data.router import DataRouter

from ..conftest import FakeProvider


class _MalformedQuoteProvider(FakeProvider):
    """A provider whose upstream sentineled an unavailable price as a non-numeric
    string (e.g. finnhub `{"c": "N/A"}` during a data hiccup), so quote() raises
    decimal.InvalidOperation — an ArithmeticError, NOT a ProviderError."""

    async def quote(self, symbol: str) -> Quote:
        self.calls += 1
        # Faithful to finnhub.quote's `last = Decimal(str(current))`: the guard
        # `if current in (None, 0)` does not catch a non-numeric string, so the
        # Decimal conversion raises decimal.InvalidOperation before any Quote is
        # built. InvalidOperation subclasses ArithmeticError, not ValueError.
        raw_price_field = "N/A"  # what the upstream JSON returned for the price
        last = Decimal(str(raw_price_field))
        return Quote(symbol=symbol, bid=last, ask=last, last=last,  # unreachable
                     as_of=datetime.now(UTC), source=self.name)


# F011: a provider quote() that raises a non-PoseidonError (decimal.InvalidOperation
# from an "N/A" price, an ArithmeticError not a ProviderError) must be treated as a
# provider failure and fail over. Pre-fix it escaped _route (matched neither
# `except ProviderError` nor `except NotImplementedError`), so failover never happened
# and record_failure was never called. Guards router.py's new
# `except (ArithmeticError, TypeError, ValueError)` branch.
async def test_f011_invalid_operation_fails_over_and_penalizes() -> None:
    bad = _MalformedQuoteProvider(name="bad")
    good = FakeProvider(name="good", price="200.00")
    # Lower priority number is tried first (see test_data_router.test_failover_on_error),
    # so `bad` (10) is hit before `good` (20): its InvalidOperation must fail over to
    # `good` rather than escaping the router.
    router = DataRouter([(bad, 10), (good, 20)], FreshnessPolicy())

    quote = await router.quote("AAPL", allow_delayed=True)

    # (1) Failed over to the good provider instead of the InvalidOperation escaping.
    assert quote.source == "good"
    assert quote.last == Decimal("200.00")
    assert bad.calls == 1  # the bad provider WAS tried first (and raised)

    # (2) The malformed provider was penalized (record_failure), not silently bypassed.
    # consecutive_failures is 0 pre-fix (record_failure never runs for InvalidOperation).
    bad_status = next(s for s in router.provider_status() if s["name"] == "bad")
    assert bad_status["consecutive_failures"] == 1
    assert bad_status["available"] is False  # in the penalty box after the failure
