"""Symbol classification: distinguish crypto pairs from equity tickers.

Lives in ``core/`` (not ``data/``) because this pure domain classification is
used by three layers — data routing, execution/api order tagging, and provider
parsing — and ``core`` may not import any of them.

The canonical internal form for a crypto symbol is ``BASE/QUOTE``, uppercase,
with exactly one ``/`` (e.g. ``BTC/USD``). This matches both Alpaca's crypto
data API (``v1beta3``, ``symbols=BTC/USD``) and its trading API, so no per-layer
remapping is needed. No equity ticker contains ``/``, so "has a slash" is a
conservative, maintenance-free routing signal.
"""

from __future__ import annotations

import re

from poseidon.core.enums import AssetClass
from poseidon.core.errors import UnsupportedSymbolError

# BASE = 1..15 uppercase alphanumerics; QUOTE = 3..5 uppercase letters; one '/'.
_CRYPTO_RE = re.compile(r"^[A-Z0-9]{1,15}/[A-Z]{3,5}$")

# Only USD-quoted spot pairs are supported; stablecoin quotes are excluded.
SUPPORTED_CRYPTO_QUOTES: frozenset[str] = frozenset({"USD"})


def is_crypto_symbol(symbol: str) -> bool:
    """True iff ``symbol`` is a crypto PAIR (contains one ``/``).

    No equity ticker contains ``/``, so this is a conservative, maintenance-free
    routing signal.
    """
    return bool(_CRYPTO_RE.match(symbol.strip().upper()))


def asset_class_for_symbol(symbol: str) -> AssetClass:
    """Map a symbol to its asset class by shape (crypto pair vs equity ticker)."""
    return AssetClass.CRYPTO if is_crypto_symbol(symbol) else AssetClass.EQUITY


def normalize_crypto_symbol(symbol: str) -> str:
    """Canonicalize a crypto symbol and reject unsupported pairs cleanly.

    USDT/USDC and any non-USD quote, or a bare base with no quote, raise
    :class:`UnsupportedSymbolError` (a :class:`PoseidonError` subclass) so a
    fat-fingered pair gives a clear rejection rather than a downstream 404.
    """
    s = symbol.strip().upper()
    base, _, quote = s.partition("/")
    if not base or quote not in SUPPORTED_CRYPTO_QUOTES:
        raise UnsupportedSymbolError(
            f"{symbol!r}: only BASE/USD crypto pairs are supported "
            f"(stablecoin/{quote or '?'}-quoted pairs are not)"
        )
    return s


def canonical_crypto_pair(symbol: str) -> str:
    """Canonical ``BASE/QUOTE`` form for a symbol a BROKER already tags as crypto.

    Alpaca's positions endpoint returns crypto pairs slashless (``USDTUSD``)
    while its trading/data APIs — and this platform's canonical form — use
    ``USDT/USD``. Mapped raw, one position splits across two ledger keys: the
    exit order cannot match it (reduce-only sees 0 closable, so the platform
    refuses the sell as a would-be short) and its quote cannot route to the
    crypto-capable provider. Slashless BASE+supported-quote forms gain the
    slash; anything else passes through unchanged — this function never
    guesses about a shape it cannot split safely.
    """
    s = symbol.strip().upper()
    if "/" in s:
        return s
    for quote in SUPPORTED_CRYPTO_QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}/{quote}"
    return s
