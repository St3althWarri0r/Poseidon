"""Decision parsing and prompt hygiene for the Claude agent."""

from __future__ import annotations

from decimal import Decimal

from poseidon.ai.agent import SYSTEM_PROMPT, ClaudeAgent
from poseidon.ai.schemas import ALL_TOOLS, SUBMIT_DECISION_TOOL
from poseidon.core.config import AIConfig
from poseidon.core.enums import DecisionAction, OrderSide

from .backend_fakes import FakeBackend


def make_agent() -> ClaudeAgent:
    class _Dispatcher:
        sources_used: set[str] = {"polygon"}

    return ClaudeAgent(AIConfig(), FakeBackend([]), _Dispatcher())  # type: ignore[arg-type]


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


INVALIDATION = "close below 172.50 on above-average volume, or the catalyst prints a miss"

_TRADE = {"symbol": "aapl", "asset_class": "equity", "side": "buy",
          "order_type": "limit", "quantity": "10", "limit_price": "100.50",
          "stop_price": None, "time_in_force": "day", "strategy": "momentum"}


def test_rationale_invalidation_round_trips() -> None:
    agent = make_agent()
    decision = agent._parse_decision(
        {"action": "buy", "trades": [dict(_TRADE)],
         "rationale": {**RATIONALE, "invalidation": INVALIDATION},
         "data_gaps": [], "summary": "s"},
        "cycle5", "m",
    )
    assert decision.rationale is not None
    assert decision.rationale.invalidation == INVALIDATION
    assert decision.trades  # risk case present -> trades intact


def test_missing_invalidation_defaults_empty_and_keeps_trades() -> None:
    # Decisions stored before the field existed, and weak local models that
    # omit it, must not void trades: invalidation is advisory context for
    # reflection and the operator, not execution data.
    agent = make_agent()
    decision = agent._parse_decision(
        {"action": "buy", "trades": [dict(_TRADE)], "rationale": dict(RATIONALE),
         "data_gaps": [], "summary": "s"},
        "cycle6", "m",
    )
    assert decision.trades and decision.rationale is not None
    assert decision.rationale.invalidation == ""


def test_non_string_invalidation_degrades_to_empty_not_void() -> None:
    # Execution-relevant malformations void trades; the advisory risk-case
    # field must never have that power.
    agent = make_agent()
    decision = agent._parse_decision(
        {"action": "buy", "trades": [dict(_TRADE)],
         "rationale": {**RATIONALE, "invalidation": ["not", "a", "string"]},
         "data_gaps": [], "summary": "s"},
        "cycle7", "m",
    )
    assert decision.trades and decision.rationale is not None
    assert decision.rationale.invalidation == ""


def test_whitespace_invalidation_normalizes_to_empty() -> None:
    # Whitespace-only is "not recorded": it must not defeat the
    # only-render-when-present guards downstream.
    agent = make_agent()
    decision = agent._parse_decision(
        {"action": "hold", "trades": [], "rationale": {**RATIONALE, "invalidation": "   "},
         "data_gaps": [], "summary": "s"},
        "cycle8", "m",
    )
    assert decision.rationale is not None
    assert decision.rationale.invalidation == ""


def test_schema_requires_invalidation() -> None:
    rationale_schema = SUBMIT_DECISION_TOOL["input_schema"]["properties"]["rationale"]["anyOf"][0]
    assert "invalidation" in rationale_schema["properties"]
    assert "invalidation" in rationale_schema["required"]
    # Strict tools demand a COMPLETE required list (ai/CLAUDE.md): a property
    # added without its required entry breaks strict validation at the API
    # with no local signal. Pin completeness at every hand-maintained level.
    assert set(rationale_schema["required"]) == set(rationale_schema["properties"])
    top = SUBMIT_DECISION_TOOL["input_schema"]
    assert set(top["required"]) == set(top["properties"])
    trade_schema = top["properties"]["trades"]["items"]
    assert set(trade_schema["required"]) == set(trade_schema["properties"])


def test_system_prompt_carries_sizing_and_risk_case_discipline() -> None:
    # The conviction-scaled sizing contract: baseline from suggest_position_size,
    # size expresses confidence, and the armed stop mechanizes the stated
    # invalidation condition.
    lower = SYSTEM_PROMPT.lower()
    assert "position sizing" in lower
    assert "suggest_position_size" in SYSTEM_PROMPT
    assert "invalidation" in lower
