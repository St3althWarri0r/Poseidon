"""Weak-local-model robustness pins for the ChatBackend seam (v2.8.0).

The OpenAI-compatible backend has no server-side strict-tool enforcement, so a
weaker local model (Devstral-24B) can emit self-contradictory or malformed
``submit_decision`` payloads that the strict-schema Anthropic path never
produced. These pin the three confirmed pre-merge-review findings:

  1. a no-trade ``action`` (no_action/hold) that still carries a populated
     ``trades[]`` must NOT execute — the trades are voided;
  2. a malformed/partial decision payload must degrade to a graceful no_action
     Decision, never raise out of the cycle (which also skips usage metering);
  3. a 2xx response whose JSON body is not an object must surface as AgentError,
     not an AttributeError that escapes the AgentError channel callers handle.
"""
from __future__ import annotations

import httpx
import pytest

from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.backends.base import ToolCall
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.core.config import AIConfig
from poseidon.core.enums import DecisionAction, TradingMode
from poseidon.core.errors import AgentError

from .backend_fakes import FakeBackend, tool_use

VALID_TRADE = {
    "symbol": "AAPL", "side": "buy", "order_type": "limit", "quantity": "10",
    "limit_price": "150.00", "asset_class": "equity", "time_in_force": "day",
    "strategy": "test",
}
VALID_RATIONALE = {
    "thesis": "t", "timing": "now", "expected_edge": "e", "risk": "r", "reward": "w",
    "confidence": 0.6, "supporting_indicators": [], "supporting_news": [],
    "portfolio_impact": "small", "exit_plan": {"stop_loss": "140", "take_profit": "160"},
    "max_expected_loss": "1%", "alternative_scenarios": [],
}


class _Dispatcher:
    def __init__(self) -> None:
        self.sources_used: set[str] = set()

    def reset_cycle_budget(self) -> None:
        pass

    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        self.sources_used.add("fake")
        return ('{"ok": true}', False)


def _agent(responses: list) -> ClaudeAgent:
    return ClaudeAgent(AIConfig(), FakeBackend(responses), _Dispatcher())  # type: ignore[arg-type]


async def _decide(payload) -> object:
    responses = [tool_use(ToolCall("d1", "submit_decision", payload))]  # type: ignore[arg-type]
    return await _agent(responses).run_cycle(
        mode=TradingMode.RESEARCH, watchlist=["AAPL"], enabled_strategies=[],
        strategy_signals=[], market_session="regular")


# -- Finding 1: action/trades coupling --------------------------------------

async def test_no_action_carrying_trades_voids_them() -> None:
    d = await _decide({"action": "no_action", "trades": [VALID_TRADE],
                       "rationale": VALID_RATIONALE, "summary": "self-contradictory"})
    assert d.action == DecisionAction.NO_ACTION
    assert d.trades == []  # a no_action decision must never carry executable trades


async def test_hold_carrying_trades_voids_them() -> None:
    d = await _decide({"action": "hold", "trades": [VALID_TRADE],
                       "rationale": VALID_RATIONALE, "summary": "hold but with a trade"})
    assert d.action == DecisionAction.HOLD
    assert d.trades == []


async def test_buy_with_valid_trade_still_executes() -> None:
    # Guardrail: the coupling fix must not void legitimate trade-bearing actions.
    d = await _decide({"action": "buy", "trades": [VALID_TRADE],
                       "rationale": VALID_RATIONALE, "summary": "real buy"})
    assert d.action == DecisionAction.BUY
    assert len(d.trades) == 1


# -- Finding 2: malformed payloads degrade to no_action, never crash --------

async def test_non_object_payload_degrades_to_no_action() -> None:
    d = await _decide([])  # submit_decision arguments decoded to a list, not an object
    assert d.action == DecisionAction.NO_ACTION
    assert d.trades == []


async def test_unknown_action_string_degrades_to_no_action() -> None:
    d = await _decide({"action": "hold_position", "trades": [], "summary": "bad enum"})
    assert d.action == DecisionAction.NO_ACTION


async def test_malformed_rationale_voids_trades_without_crashing() -> None:
    d = await _decide({"action": "buy", "trades": [VALID_TRADE],
                       "rationale": {**VALID_RATIONALE, "confidence": "high"},
                       "summary": "non-numeric confidence"})
    assert d.trades == []  # rationale unbuildable -> voided -> trades voided, no raise


async def test_non_dict_rationale_voids_trades_without_crashing() -> None:
    d = await _decide({"action": "buy", "trades": [VALID_TRADE],
                       "rationale": "looks good to me", "summary": "rationale is a string"})
    assert d.trades == []


async def test_trades_as_single_object_degrades_not_crash() -> None:
    # The single most common weak-model error: `trades` is one object, not an array.
    d = await _decide({"action": "buy",
                       "trades": {"symbol": "AAPL", "side": "buy", "quantity": "10"},
                       "rationale": VALID_RATIONALE, "summary": "trades not a list"})
    assert d.action == DecisionAction.BUY
    assert d.trades == []


async def test_trades_as_scalar_degrades_not_crash() -> None:
    d = await _decide({"action": "buy", "trades": 5, "rationale": VALID_RATIONALE, "summary": "x"})
    assert d.trades == []


async def test_non_dict_trade_element_voids_without_crashing() -> None:
    d = await _decide({"action": "buy", "trades": ["AAPL", VALID_TRADE],
                       "rationale": VALID_RATIONALE, "summary": "bare string element"})
    assert d.trades == []  # any malformed element voids all coupled trades


async def test_non_list_data_gaps_does_not_crash() -> None:
    d = await _decide({"action": "no_action", "trades": [], "data_gaps": 5, "summary": "x"})
    assert d.action == DecisionAction.NO_ACTION


# -- Finding 3: non-object 2xx body -> AgentError ---------------------------

def _oai_cfg() -> AIConfig:
    return AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1",
                    model="devstral", temperature=0.2)


def _backend(handler) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(_oai_cfg(), transport=httpx.MockTransport(handler))


async def test_non_object_2xx_body_becomes_agent_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])  # valid JSON, but not an object

    with pytest.raises(AgentError):
        await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")


async def test_non_object_tool_arguments_dropped_not_forwarded() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "d", "choices": [{
            "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "submit_decision", "arguments": "[]"}}]}}],
            "usage": {}})

    resp = await _backend(handler).complete([{"role": "user", "content": "x"}], tools=[], system="s")
    assert resp.tool_calls == []  # non-object args are dropped, not passed downstream
