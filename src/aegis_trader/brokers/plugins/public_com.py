"""Public.com — documented plugin stub.

Status: Public launched an official trading API for individual investors
(https://public.com/api). It is a genuine, supported interface; access is
enabled per-account from Public's settings and the API surface (equities,
options, order placement) maps cleanly onto Aegis's Broker interface.

This ships as a documented scaffold rather than a full implementation
because the API is new and its endpoint contract is still evolving;
implementing against a moving spec from memory would risk silently wrong
order semantics — the one place Aegis must never guess. The integration
checklist:

  1. Enable API access in your Public account and generate a secret key.
  2. Store {"api_secret": "...", "account_id": "..."} in the vault under a
     `public` credential.
  3. Implement token exchange + the account/order endpoints from the
     current docs at public.com/api in this module (the Broker interface
     in brokers/base.py defines exactly what is needed).

See docs/plugin-development.md for the walkthrough.
"""

from __future__ import annotations

from ..base import UnsupportedBroker


class PublicBroker(UnsupportedBroker):
    name = "public"
    display_name = "Public.com"
    reason = (
        "Public's official trading API exists; this scaffold awaits "
        "implementation against the current endpoint contract "
        "(see module docstring for the checklist)."
    )
