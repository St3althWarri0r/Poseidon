"""E*TRADE — documented plugin stub.

Status: E*TRADE (Morgan Stanley) offers an official API
(https://developer.etrade.com) covering accounts, quotes, and order
placement. It authenticates with OAuth 1.0a and requires a *daily*
interactive re-authorization: request tokens expire at midnight ET every
day and renewing them requires the user to visit an E*TRADE consent page.

That daily human-in-the-loop step is incompatible with unattended 24/7
autonomous operation, so this ships as a documented scaffold rather than a
half-working integration. If you accept the daily re-auth ritual, the
integration checklist is:

  1. Register a consumer key/secret at developer.etrade.com and store
     {"consumer_key": "...", "consumer_secret": "..."} in the vault under
     an `etrade` credential.
  2. Implement the OAuth 1.0a HMAC-SHA1 signing flow (request token ->
     user authorize URL -> access token) plus token renewal, and the
     /v1/accounts and /v1/accounts/{id}/orders endpoints in this module.
  3. Add an `poseidon broker etrade-auth` CLI step for the daily consent.

See docs/plugin-development.md for the Broker interface contract.
"""

from __future__ import annotations

from ..base import UnsupportedBroker


class ETradeBroker(UnsupportedBroker):
    name = "etrade"
    display_name = "E*TRADE"
    reason = (
        "E*TRADE's official API requires an interactive OAuth 1.0a "
        "re-authorization every trading day, which is incompatible with "
        "unattended autonomous operation; a scaffold and checklist are "
        "provided for users who accept the daily re-auth."
    )
