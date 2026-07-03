"""Webull — documented plugin stub.

Status: Webull operates an official "Webull OpenAPI" developer program
(https://developer.webull.com) with REST/SDK access to quotes and trading,
but access requires an application and individual approval, regional
availability varies, and the equity-trading scopes are not open to all
retail accounts. Because Poseidon cannot assume an approved OpenAPI
subscription, this ships as a stub with the integration path documented.

To integrate after you are approved for Webull OpenAPI:
  1. Store {"app_key": "...", "app_secret": "...", "account_id": "..."}
     in the vault under a `webull` credential.
  2. Implement the signed-request scheme from Webull's OpenAPI docs in this
     module (HMAC-SHA1 request signing, documented in their developer
     portal) following docs/plugin-development.md.

Unofficial clients that emulate the Webull app are not supported.
"""

from __future__ import annotations

from ..base import UnsupportedBroker


class WebullBroker(UnsupportedBroker):
    name = "webull"
    display_name = "Webull"
    reason = (
        "Webull's official OpenAPI exists but is application-gated and not "
        "generally available to retail accounts; integrate it here once your "
        "OpenAPI access is approved."
    )
