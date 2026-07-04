"""Vanguard — documented stub.

Vanguard offers no public, self-service trading API of any kind (no
official REST API, no OAuth developer program). Poseidon therefore cannot
sync or trade a Vanguard account: doing so would require screen-scraping
or reverse-engineered private endpoints, which this project categorically
refuses (terms violations, fragile, unsafe with real money).

If Vanguard ships an official retail API, the integration checklist is:
token/OAuth auth in ``connect``; account + positions mapping; idempotent
``submit_order`` with client order IDs; ``cancel_order``/``order_status``/
``open_orders``. Until then, free alternatives with official APIs:
Alpaca, Public.com, Tradier, tastytrade, Schwab, IBKR.
"""

from __future__ import annotations

from ..base import UnsupportedBroker


class VanguardBroker(UnsupportedBroker):
    name = "vanguard"
    display_name = "Vanguard"
    reason = ("Vanguard has never offered a public trading API; there is no "
              "compliant way to automate a Vanguard brokerage account.")
