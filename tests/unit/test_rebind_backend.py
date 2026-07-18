"""Live-swap seam: ClaudeAgent/ChatService.rebind_backend point the frozen
backend ref at a new object without re-running _wire_ai.

These are the only two objects that hold a frozen ``self._backend`` (reflection
and analysis resolve the utility backend at call time via a lambda, and the
reviewer reads ``kernel._backend`` fresh each call). After a rebind, the next
``run_cycle`` / ``send`` must drive the NEW backend and never touch the old one.
"""
from __future__ import annotations

import pytest

from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.chat import ChatService
from poseidon.core.config import AIConfig
from poseidon.core.enums import TradingMode
from poseidon.storage.db import Database

from .backend_fakes import FakeBackend, text_end


@pytest.fixture
async def chat_db(tmp_path):
    db = Database(tmp_path / "rebind_chat.db")
    await db.open()
    yield db
    await db.close()


class _Dispatcher:
    def __init__(self) -> None:
        self.sources_used: set[str] = set()
        self.dispatched: list[str] = []

    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        self.dispatched.append(name)
        return ('{"ok": true}', False)


async def test_agent_rebind_backend_drives_new_not_old() -> None:
    old = FakeBackend([text_end("stale")])
    new = FakeBackend([text_end("fresh")])
    agent = ClaudeAgent(AIConfig(), old, _Dispatcher())  # type: ignore[arg-type]

    agent.rebind_backend(new)  # type: ignore[arg-type]
    assert agent.backend is new  # the read-only property follows the rebind too

    await agent.run_cycle(mode=TradingMode.RESEARCH, watchlist=["AAPL"],
                          enabled_strategies=[], strategy_signals=[],
                          market_session="regular")

    assert len(new.calls) == 1  # the cycle drove the new backend
    assert old.calls == []      # ...and never the old one


async def test_chat_rebind_backend_drives_new_not_old(chat_db) -> None:
    old = FakeBackend([text_end("stale")])
    new = FakeBackend([text_end("fresh")])
    chat = ChatService(AIConfig(), old, _Dispatcher(), chat_db)  # type: ignore[arg-type]

    chat.rebind_backend(new)  # type: ignore[arg-type]

    result = await chat.send("how is AAPL?", context="mode: research")
    assert result["reply"] == "fresh"
    assert len(new.calls) == 1  # the send drove the new backend
    assert old.calls == []      # ...and never the old one
