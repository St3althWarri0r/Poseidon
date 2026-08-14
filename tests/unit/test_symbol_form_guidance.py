"""A wrong-shaped crypto symbol should teach the model, not just fail.

gpt-oss-20b asks for crypto in several shapes. Observed live in `data_gaps`:

    "ADA quote unavailable"        (bare base — no quote currency)
    "AAVE quote unavailable"       (bare base)
    "BTCUSD quote unavailable"     (slashless)

Only the slashless form was handled. A bare base routes as an equity ticker,
finds nothing, and the cycle spends a tool call on a `data_gap` that says
"unavailable" — which is *true but useless*: the data exists, the symbol shape
was wrong, and nothing told the model so.

Two different problems, deliberately fixed two different ways:

* ``ADA-USD`` (hyphen) was silently MANGLED to ``ADA-/USD`` — a real bug in
  `canonical_crypto_pair`, unambiguous to fix.
* A bare ``ADA`` is AMBIGUOUS: rewriting it to ``ADA/USD`` would hijack any
  real equity ticker of the same name. So it is not rewritten. The error
  message names the correct form instead, and the model can retry within the
  same cycle. Guessing is exactly what `ai/CLAUDE.md` forbids of a tool.
"""

from __future__ import annotations

from poseidon.core.symbols import canonical_crypto_pair, crypto_form_hint


def test_hyphen_form_is_no_longer_mangled() -> None:
    assert canonical_crypto_pair("ADA-USD") == "ADA/USD"
    assert canonical_crypto_pair("btc-usd") == "BTC/USD"


def test_slashless_still_works() -> None:
    assert canonical_crypto_pair("BTCUSD") == "BTC/USD"


def test_already_canonical_is_untouched() -> None:
    assert canonical_crypto_pair("BTC/USD") == "BTC/USD"


def test_a_bare_base_is_not_silently_rewritten() -> None:
    """Rewriting would hijack an equity ticker of the same name."""
    assert canonical_crypto_pair("ADA") == "ADA"


def test_a_bare_crypto_base_gets_an_actionable_hint() -> None:
    hint = crypto_form_hint("ADA")
    assert hint is not None
    assert "ADA/USD" in hint


def test_the_hint_covers_the_symbols_seen_live() -> None:
    for base in ("ADA", "AAVE", "LTC", "SUI"):
        hint = crypto_form_hint(base)
        assert hint is not None and f"{base}/USD" in hint, base


def test_no_hint_for_an_ordinary_equity_ticker() -> None:
    """A hint on every failed equity quote would be noise, and wrong."""
    for ticker in ("AAPL", "MSFT", "SPY", "NVDA"):
        assert crypto_form_hint(ticker) is None, ticker


def test_no_hint_for_an_already_correct_pair() -> None:
    assert crypto_form_hint("BTC/USD") is None
