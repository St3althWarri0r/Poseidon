"""Repeat-call guard and stall abort for the PM's tool loop.

Live pathology (10 cycles in one overnight session): the local model falls
into a ``get_portfolio``/``get_risk_status`` alternation and burns all 24 tool
iterations on calls it has already made — small no-arg results, so the
cumulative char budgets never trip. Three layers close it:

  * ``ToolDispatcher`` counts exact (tool, args) repeats per cycle and attaches
    an explicit ``repeat_note`` to the second and later identical calls — the
    REAL live result is still returned (anti-starvation, live-data invariant);
  * ``ClaudeAgent.run_cycle`` aborts to a no-action decision after
    ``max_stalled_iterations`` consecutive iterations made only repeat calls;
  * ``ChatService.send`` resets the per-cycle budget (and repeat counts) per
    operator message — without that, the chat dispatcher's cumulative counter
    spans the whole process lifetime and permanently exhausts chat data tools.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.backends.base import ToolCall
from poseidon.ai.chat import ChatService
from poseidon.ai.tools import ToolDispatcher
from poseidon.core.config import AIConfig, CycleBudgetConfig
from poseidon.core.enums import DecisionAction, TradingMode
from poseidon.core.models import Bar
from poseidon.storage.db import Database

from .backend_fakes import FakeBackend, text_end, tool_use

# ---------------------------------------------------------------- fixtures


def _bars(n: int) -> list[Bar]:
    out: list[Bar] = []
    day0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        close = Decimal("100.00") + Decimal(i) * Decimal("0.25")
        start = day0 + timedelta(days=i)
        out.append(Bar(symbol="AAPL", open=close - Decimal("0.50"),
                       high=close + Decimal("1.00"), low=close - Decimal("1.00"),
                       close=close, volume=1000 + i, start=start,
                       end=start + timedelta(days=1), source="barsrc"))
    return out


class _Router:
    def __init__(self, bars: list[Bar] | None = None) -> None:
        self._bars = bars if bars is not None else _bars(5)

    async def bars(self, symbol: str, timeframe: str = "1d", limit: int = 250) -> list[Bar]:
        return self._bars


def _disp(budget: CycleBudgetConfig | None = None) -> ToolDispatcher:
    return ToolDispatcher(_Router(), None, None,  # type: ignore[arg-type]
                          allow_delayed_quotes=True, budget=budget)


_BARS_ARGS = {"symbol": "AAPL", "timeframe": "1d", "limit": 5}


# ------------------------------------------------- dispatcher repeat_note


async def test_first_call_carries_no_repeat_note() -> None:
    disp = _disp()
    payload, is_error = await disp.dispatch("get_bars", dict(_BARS_ARGS))
    assert not is_error
    assert "repeat_note" not in json.loads(payload)


async def test_identical_repeat_gets_numbered_note_and_real_data() -> None:
    disp = _disp()
    await disp.dispatch("get_bars", dict(_BARS_ARGS))
    payload, is_error = await disp.dispatch("get_bars", dict(_BARS_ARGS))
    assert not is_error
    result = json.loads(payload)
    assert "#2" in result["repeat_note"]
    # Anti-starvation: the note annotates, it never replaces the live data.
    assert len(result["bars"]) == 5
    payload, _ = await disp.dispatch("get_bars", dict(_BARS_ARGS))
    assert "#3" in json.loads(payload)["repeat_note"]


async def test_different_args_are_not_a_repeat() -> None:
    disp = _disp()
    await disp.dispatch("get_bars", dict(_BARS_ARGS))
    payload, _ = await disp.dispatch("get_bars", {**_BARS_ARGS, "timeframe": "1h"})
    assert "repeat_note" not in json.loads(payload)


async def test_reset_cycle_budget_clears_repeat_counts() -> None:
    disp = _disp()
    await disp.dispatch("get_bars", dict(_BARS_ARGS))
    disp.reset_cycle_budget()
    payload, _ = await disp.dispatch("get_bars", dict(_BARS_ARGS))
    assert "repeat_note" not in json.loads(payload)


# ------------------------------------------------- agent stall abort


class _Dispatcher:
    def __init__(self) -> None:
        self.sources_used: set[str] = set()

    def reset_cycle_budget(self) -> None:
        pass

    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        return ('{"ok": true}', False)


def _agent(responses: list, **cfg: int) -> ClaudeAgent:
    return ClaudeAgent(AIConfig(**cfg), FakeBackend(responses), _Dispatcher())  # type: ignore[arg-type]


async def _run(agent: ClaudeAgent):
    return await agent.run_cycle(mode=TradingMode.RESEARCH, watchlist=["AAPL"],
                                 enabled_strategies=[], strategy_signals=[],
                                 market_session="regular")


async def test_alternating_repeat_loop_aborts_early() -> None:
    # The live pathology verbatim: portfolio/risk alternation. Iterations 1-2
    # request new data (stalled resets), 3-5 only repeat -> abort at the default
    # max_stalled_iterations=3 instead of burning to max_tool_iterations.
    responses = []
    for _ in range(10):
        responses.append(tool_use(ToolCall("t", "get_portfolio", {})))
        responses.append(tool_use(ToolCall("t", "get_risk_status", {})))
    backend = FakeBackend(responses)
    agent = ClaudeAgent(AIConfig(), backend, _Dispatcher())  # type: ignore[arg-type]
    d = await _run(agent)
    assert d.action == DecisionAction.NO_ACTION
    assert "repeat" in d.summary.lower()
    assert len(backend.calls) == 5  # 2 novel iterations + 3 stalled, not 24


async def test_new_request_resets_the_stall_counter() -> None:
    quote_a = tool_use(ToolCall("t", "get_quote", {"symbol": "AAPL"}))
    responses = [
        quote_a, quote_a, quote_a,  # 1 novel + 2 stalled — one short of abort
        tool_use(ToolCall("t", "get_quote", {"symbol": "MSFT"})),  # novel -> reset
        tool_use(ToolCall("d", "submit_decision",
                          {"action": "no_action", "trades": [], "summary": "flat"})),
    ]
    d = await _run(_agent(responses))
    # The submitted decision arrived — the stall guard never fired.
    assert d.summary == "flat"


async def test_stall_abort_disabled_falls_through_to_iteration_limit() -> None:
    repeat = tool_use(ToolCall("t", "get_portfolio", {}))
    backend = FakeBackend([repeat] * 10)
    agent = ClaudeAgent(AIConfig(max_tool_iterations=6, max_stalled_iterations=0),
                        backend, _Dispatcher())  # type: ignore[arg-type]
    d = await _run(agent)
    assert d.action == DecisionAction.NO_ACTION
    assert "limit" in d.summary.lower()
    assert len(backend.calls) == 6


# ------------------------------------------------- chat per-message reset


class _SpyDispatcher(_Dispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.resets = 0

    def reset_cycle_budget(self) -> None:
        self.resets += 1


async def test_chat_send_resets_budget_per_message(tmp_path) -> None:
    db = Database(tmp_path / "chat.db")
    await db.open()
    try:
        dispatcher = _SpyDispatcher()
        chat = ChatService(AIConfig(), FakeBackend([text_end("hi"), text_end("again")]),
                           dispatcher, db)  # type: ignore[arg-type]
        await chat.send("hello", context="c")
        assert dispatcher.resets == 1
        await chat.send("more", context="c")
        assert dispatcher.resets == 2
    finally:
        await db.close()
