"""Fidelity — documented plugin stub.

Status (verified against Fidelity's public developer materials): Fidelity
does not offer an official self-service trading API for individual retail
customers. Fidelity Access / the Fidelity Integration Xchange exist for
vetted institutional partners and data aggregators (read-only account
aggregation), not for retail order placement.

Aegis therefore ships this stub, which refuses every operation with an
explanation, rather than automating the website or mobile app — screen
scraping violates Fidelity's terms of use and is operationally fragile.

If Fidelity publishes a retail trading API, implement it here following
docs/plugin-development.md; the rest of the platform needs no changes.
"""

from __future__ import annotations

from ..base import UnsupportedBroker


class FidelityBroker(UnsupportedBroker):
    name = "fidelity"
    display_name = "Fidelity"
    reason = (
        "Fidelity offers no self-service trading API for retail customers; "
        "its partner APIs (Fidelity Access / Integration Xchange) are "
        "aggregation-only and gated to vetted institutions."
    )
