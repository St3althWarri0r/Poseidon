"""M1 Finance — documented plugin stub.

Status: M1 Finance publishes no official public API for account access or
trading. There is no developer program, sandbox, or documented automation
interface. Community projects that drive M1's private GraphQL endpoints
are unsupported, violate M1's terms, and break without notice — Poseidon will
not use them.

If M1 ships an official API, implement it here per
docs/plugin-development.md.
"""

from __future__ import annotations

from ..base import UnsupportedBroker


class M1FinanceBroker(UnsupportedBroker):
    name = "m1finance"
    display_name = "M1 Finance"
    reason = "M1 Finance has no official public API or developer program of any kind."
