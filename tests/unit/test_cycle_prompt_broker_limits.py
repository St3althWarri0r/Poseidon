"""Broker per-order caps belong in the cycle prompt, not behind an optional tool.

PR #28 surfaced Alpaca's $200k per-order crypto cap through `get_risk_status`.
That only helps if the model chooses to call it. The cycle prompt carried no
limits at all, and the system prompt merely mentions the tool in prose — so a
model that skipped the call sized from its own arithmetic, proposed an over-cap
order, and got a broker-side preflight rejection *after* `submit_decision` had
already ended the cycle. Nothing carried that rejection into the next cycle
either (`get_portfolio` returns no rejected orders; the reflection lesson map
has no `rejected_broker` entry), so the same order was re-proposed indefinitely.

With a local 20B model, skipping an optional tool call is the expected case, not
the edge case. Putting the caps in the prompt is deterministic: the model cannot
fail to receive them.
"""

from __future__ import annotations

from poseidon.ai.agent import ClaudeAgent
from poseidon.core.enums import TradingMode

LIMITS = {
    "max_order_notional": {"crypto": "200000"},
    "note": "alpaca refuses any single crypto order (buy OR sell) above $200,000 notional",
}


def _prompt(**kw: object) -> str:
    base: dict[str, object] = {
        "cycle_id": "c1", "mode": TradingMode.AUTONOMOUS, "watchlist": ["BTC/USD"],
        "enabled_strategies": ["momentum"], "strategy_signals": [],
        "market_session": "regular",
    }
    base.update(kw)
    return ClaudeAgent._cycle_prompt(**base)  # type: ignore[arg-type]


def test_broker_limits_appear_in_the_cycle_prompt() -> None:
    out = _prompt(broker_limits=LIMITS)
    assert "200000" in out, "the per-order cap must reach the model without a tool call"


def test_the_caps_note_is_carried_verbatim() -> None:
    """The note explains that an oversized POSITION must be exited in slices —
    which is the part that stops the model re-proposing an un-exitable size."""
    out = _prompt(broker_limits=LIMITS)
    assert "alpaca refuses" in out


def test_absent_limits_render_no_block() -> None:
    """A broker declaring no limits (order_limits() -> {}) must add nothing, so
    this cannot perturb a paper/tradier/non-capped setup.

    (Compared by block presence, not by whole-prompt equality: the prompt
    embeds `datetime.now(UTC)`, so two renders are never byte-equal.)
    """
    marker = "BROKER PER-ORDER CAPS"
    assert marker not in _prompt(broker_limits={})
    assert marker not in _prompt(broker_limits=None)
    assert marker not in _prompt()
    assert marker in _prompt(broker_limits=LIMITS)
