"""Crypto on a LIVE brokerage is gated, not merely discouraged.

`docs/user-guide.md` and `docs/api-configuration.md` long stated crypto was
"paper-only today". Nothing enforced it: the only broker-side check was the
`CRYPTO` capability, which the live `alpaca` and `public_com` brokers **both**
advertise (`alpaca.py`, `public_com.py`), so a crypto order on a live account
was submitted normally. The reader most at risk was the one following the
*Trading manually* section and typing `BTC/USD` into the ticket.

`risk.allow_live_crypto` (default False) makes the documented promise real
while leaving a deliberate opt-out. Paper brokers are never affected.
"""

from __future__ import annotations

import inspect

from poseidon.core.config import RiskConfig
from poseidon.execution import manager as manager_mod


def test_flag_defaults_to_refusing_live_crypto() -> None:
    assert RiskConfig().allow_live_crypto is False


def test_flag_is_opt_in_not_removable() -> None:
    assert RiskConfig(allow_live_crypto=True).allow_live_crypto is True


def test_gate_checks_paper_and_the_flag_together() -> None:
    """Pin the shape of the guard: it must test BOTH is_paper and the flag, so
    a paper broker is never blocked and a live one is never waved through on
    capability alone."""
    src = inspect.getsource(manager_mod)
    assert "allow_live_crypto" in src, "the live-crypto gate is missing"
    window = src[src.index("allow_live_crypto") - 400: src.index("allow_live_crypto") + 200]
    assert "is_paper" in window, "the gate must exempt paper brokers"
    assert "AssetClass.CRYPTO" in window, "the gate must be crypto-scoped"


def test_gate_is_separate_from_the_capability_check() -> None:
    """The capability check answers 'can this broker do crypto at all'; the gate
    answers 'may it do so with real money'. Collapsing them would re-lose the
    distinction that made the docs wrong."""
    src = inspect.getsource(manager_mod)
    assert "does not support crypto orders" in src
    assert "risk.allow_live_crypto: true" in src, "the refusal must say how to opt in"
