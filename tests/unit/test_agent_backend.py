"""ClaudeAgent.run_cycle driven over a scripted backend (no network, no model)."""
from __future__ import annotations

import pytest

from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.backends.base import ToolCall
from poseidon.core.config import AIConfig
from poseidon.core.enums import DecisionAction, TradingMode
from poseidon.core.errors import AgentRefusedError

from .backend_fakes import FakeBackend, refusal, text_end, tool_use


class _Dispatcher:
    def __init__(self) -> None:
        self.sources_used: set[str] = set()

    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        self.sources_used.add("fake")
        return ('{"ok": true}', False)


def _agent(responses: list) -> ClaudeAgent:
    return ClaudeAgent(AIConfig(), FakeBackend(responses), _Dispatcher())  # type: ignore[arg-type]


async def _run(agent: ClaudeAgent):
    return await agent.run_cycle(mode=TradingMode.RESEARCH, watchlist=["AAPL"],
                                 enabled_strategies=[], strategy_signals=[],
                                 market_session="regular")


async def test_data_tool_then_submit_decision_reaches_parse() -> None:
    responses = [
        tool_use(ToolCall("t1", "get_portfolio", {})),
        tool_use(ToolCall("d1", "submit_decision",
                          {"action": "no_action", "trades": [], "summary": "flat"})),
    ]
    d = await _run(_agent(responses))
    assert d.action == DecisionAction.NO_ACTION
    assert d.summary == "flat"
    assert d.data_sources == ["fake"]  # the dispatched tool's source was captured


async def test_no_tool_calls_is_no_action() -> None:
    d = await _run(_agent([text_end("nothing to do")]))
    assert d.action == DecisionAction.NO_ACTION


async def test_refusal_raises() -> None:
    with pytest.raises(AgentRefusedError):
        await _run(_agent([refusal()]))
