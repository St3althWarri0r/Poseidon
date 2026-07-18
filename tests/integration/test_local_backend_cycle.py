"""End-to-end review cycle over the real OpenAI-compatible backend.

Uses httpx MockTransport (no network) to script an LM-Studio-style two-round
exchange, and asserts the loop composes the backend's assistant turn and
``role: tool`` results into a well-formed history that is re-sent correctly.
"""
from __future__ import annotations

import json

import httpx

from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.core.config import AIConfig
from poseidon.core.enums import DecisionAction, TradingMode


class _Dispatcher:
    def __init__(self) -> None:
        self.sources_used: set[str] = set()

    def reset_cycle_budget(self) -> None:
        pass

    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        self.sources_used.add("alpaca")
        return (json.dumps({"cash": "42000000", "positions": []}), False)


def _script(captured: list) -> httpx.MockTransport:
    rounds = iter([
        {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "get_portfolio", "arguments": "{}"}}]}},
        {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function", "function": {
             "name": "submit_decision",
             "arguments": json.dumps({"action": "no_action", "trades": [], "summary": "flat"})}}]}},
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return httpx.Response(200, json={"model": "devstral", "choices": [next(rounds)],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    return httpx.MockTransport(handler)


async def test_local_cycle_produces_decision_and_wellformed_history() -> None:
    captured: list = []
    cfg = AIConfig(backend="openai_compatible", base_url="http://x/v1", model="devstral")
    backend = OpenAICompatibleBackend(cfg, transport=_script(captured))
    agent = ClaudeAgent(cfg, backend, _Dispatcher())  # type: ignore[arg-type]

    decision = await agent.run_cycle(mode=TradingMode.AUTONOMOUS, watchlist=["AAPL"],
                                     enabled_strategies=[], strategy_signals=[],
                                     market_session="regular")

    assert decision.action == DecisionAction.NO_ACTION
    assert decision.model == "devstral"
    assert decision.usage["api_calls"] == 2
    # The second request must carry the first round's tool result as a
    # role:tool message keyed to the originating tool_call id.
    second_request_messages = captured[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1"
               for m in second_request_messages)
    await backend.aclose()
