"""Decision parsing and prompt hygiene for the Claude agent."""

from __future__ import annotations

from decimal import Decimal

from poseidon.ai.agent import SYSTEM_PROMPT, ClaudeAgent
from poseidon.ai.schemas import ALL_TOOLS, SUBMIT_DECISION_TOOL
from poseidon.core.config import AIConfig
from poseidon.core.enums import DecisionAction, OrderSide


def make_agent() -> ClaudeAgent:
    class _Dispatcher:
        sources_used: set[str] = {"polygon"}

    return ClaudeAgent(AIConfig(), "test-key", _Dispatcher())  # type: ignore[arg-type]


RATIONALE = {
    "thesis": "t", "timing": "now", "expected_edge": "e", "risk": "r", "reward": "w",
    "confidence": 0.7, "supporting_indicators": ["rsi"], "supporting_news": [],
    "portfolio_impact": "small",
    "exit_plan": {"stop_loss": "95.00", "take_profit": "110.00", "time_stop": None, "notes": None},
    "max_expected_loss": "$500", "alternative_scenarios": ["chop"],
}


def test_parse_full_decision() -> None:
    agent = make_agent()
    decision = agent._parse_decision(
        {
            "action": "buy",
            "trades": [{
                "symbol": "aapl", "asset_class": "equity", "side": "buy",
                "order_type": "limit", "quantity": "10", "limit_price": "100.50",
                "stop_price": None, "time_in_force": "day", "strategy": "momentum",
            }],
            "rationale": RATIONALE,
            "data_gaps": [],
            "summary": "s",
        },
        "cycle1", "claude-opus-4-8",
    )
    assert decision.action is DecisionAction.BUY
    assert decision.trades[0].symbol == "AAPL"
    assert decision.trades[0].side is OrderSide.BUY
    assert decision.trades[0].limit_price == Decimal("100.50")
    assert decision.rationale is not None
    assert decision.rationale.exit_plan.stop_loss == Decimal("95.00")
    assert decision.data_sources == ["polygon"]


def test_decision_captures_data_gaps_summary_and_per_trade_exits() -> None:
    # C2 + A1 regression: data_gaps/summary must survive parsing (not be
    # silently dropped), and per-trade stop_loss/take_profit must land on the
    # ProposedTrade so the guardian can arm each symbol's own levels.
    agent = make_agent()
    decision = agent._parse_decision(
        {
            "action": "buy",
            "trades": [{
                "symbol": "AAPL", "asset_class": "equity", "side": "buy",
                "order_type": "limit", "quantity": "10", "limit_price": "100.50",
                "stop_price": None, "time_in_force": "day", "strategy": "momentum",
                "stop_loss": "95.00", "take_profit": "120.00",
            }],
            "rationale": RATIONALE,
            "data_gaps": ["no fresh options chain for AAPL"],
            "summary": "Opening a momentum long in AAPL.",
        },
        "cycle_c2", "claude-opus-4-8",
    )
    assert decision.data_gaps == ["no fresh options chain for AAPL"]
    assert decision.summary == "Opening a momentum long in AAPL."
    assert decision.trades[0].stop_loss == Decimal("95.00")
    assert decision.trades[0].take_profit == Decimal("120.00")


def test_trades_without_rationale_are_voided() -> None:
    agent = make_agent()
    decision = agent._parse_decision(
        {
            "action": "buy",
            "trades": [{
                "symbol": "AAPL", "asset_class": "equity", "side": "buy",
                "order_type": "limit", "quantity": "10", "limit_price": "100",
                "stop_price": None, "time_in_force": "day", "strategy": "momentum",
            }],
            "rationale": None, "data_gaps": [], "summary": "s",
        },
        "cycle2", "m",
    )
    assert decision.trades == []  # explainability is mandatory


def test_malformed_trade_dropped_not_fatal() -> None:
    agent = make_agent()
    decision = agent._parse_decision(
        {
            "action": "buy",
            "trades": [{"symbol": "AAPL", "side": "buy", "quantity": "not-a-number",
                        "asset_class": "equity", "order_type": "limit", "limit_price": None,
                        "stop_price": None, "time_in_force": "day", "strategy": ""}],
            "rationale": RATIONALE, "data_gaps": [], "summary": "s",
        },
        "cycle3", "m",
    )
    assert decision.trades == []


def test_confidence_clamped() -> None:
    agent = make_agent()
    rationale = dict(RATIONALE, confidence=7.5)
    decision = agent._parse_decision(
        {"action": "hold", "trades": [], "rationale": rationale, "data_gaps": [], "summary": "s"},
        "cycle4", "m",
    )
    assert decision.rationale is not None and decision.rationale.confidence == 1.0


def test_schema_and_prompt_contracts() -> None:
    # submit_decision is strict, and it's included exactly once in ALL_TOOLS.
    assert SUBMIT_DECISION_TOOL["strict"] is True
    assert SUBMIT_DECISION_TOOL["input_schema"]["additionalProperties"] is False
    assert sum(1 for t in ALL_TOOLS if t["name"] == "submit_decision") == 1
    # The live-data contract is stated in the system prompt.
    assert "LIVE DATA ONLY" in SYSTEM_PROMPT
    assert "submit_decision exactly once" in SYSTEM_PROMPT
