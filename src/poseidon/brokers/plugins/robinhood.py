"""Robinhood — documented plugin stub (equities/options).

Status: Robinhood's only official public API is the Robinhood Crypto
trading API (https://docs.robinhood.com) — it covers crypto order placement
and crypto account data only. There is no official API for equities or
options trading. The widely-circulated unofficial libraries reverse
engineer the mobile app's private endpoints, which violates Robinhood's
terms and risks account restriction; Poseidon will not use them.

If your goal is crypto automation on Robinhood, an official integration is
feasible — file an issue or implement it per docs/plugin-development.md
(Ed25519-signed requests against the documented crypto endpoints). Equity
trading remains unsupported until Robinhood publishes an official API.
"""

from __future__ import annotations

from ..base import UnsupportedBroker


class RobinhoodBroker(UnsupportedBroker):
    name = "robinhood"
    display_name = "Robinhood"
    reason = (
        "Robinhood's official API covers crypto only; there is no official "
        "equities/options trading API, and unofficial private-endpoint "
        "clients violate Robinhood's terms."
    )
