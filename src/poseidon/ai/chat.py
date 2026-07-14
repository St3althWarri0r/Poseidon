"""Dashboard chat with Claude — the AI Desk's conversation surface.

The operator talks to the same Claude that runs review cycles, with the
same live-data tools (quotes, bars, chains, news, portfolio, risk,
performance, backtests) and the same live-data-only honesty rules. The
chat can NEVER trade: the submit_decision tool is not offered here, and
nothing in the dispatcher places orders. The only mutation it can perform
is propose_algorithm, which saves a draft the operator must activate.

Conversation history is persisted (chat_messages table) so a restart
keeps context; token usage is metered into the same ai_usage table as
review cycles, so the monthly budget covers chat too.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import structlog

from ..core.config import AIConfig
from ..core.errors import AgentError
from ..storage.db import Database
from .backends.base import ChatBackend, ToolResult
from .schemas import DATA_TOOLS
from .tools import ToolDispatcher

log = structlog.get_logger(__name__)

_HISTORY_TURNS = 30  # prior messages replayed to the model per send
_MAX_MESSAGE_CHARS = 8_000

# Case-insensitive and whitespace/attribute-tolerant so a pasted variant
# (<SESSION_CONTEXT>, <Session_Context>, <session_context id="x">, </ session_context>)
# cannot forge or close the trusted platform-state block. The \b keeps it
# from matching unrelated tags like <session_contextual>.
_SESSION_CONTEXT_TAG = re.compile(r"<\s*(/?)\s*session_context\b[^>]*>", re.IGNORECASE)


class ChatBusyError(RuntimeError):
    """A previous chat message is still being processed."""


CHAT_SYSTEM_PROMPT = """You are the Poseidon desk assistant — the same Claude that runs this \
platform's trading review cycles, now in conversation with the operator on their dashboard.

Poseidon is an autonomous AI trading platform. You have tools for LIVE market data (quotes, \
bars, option chains, news, earnings, economic calendar), the live portfolio, risk metrics, \
performance, execution quality, position sizing, backtests, and proposing draft algorithms.

Rules — identical to your review-cycle rules:
- LIVE DATA ONLY. Never state a price, level, or market fact from memory. If the operator \
asks about a symbol, fetch it. If a tool fails or data is unavailable, say exactly that — \
never estimate, never fill gaps with plausible numbers.
- You CANNOT place, modify, or cancel orders from this chat, and you must not imply you can. \
For trading, point the operator to the trade ticket (Portfolio view) or explain how the \
review cycle / operating modes work.
- You may save algorithm DRAFTS with propose_algorithm when asked to write one; the operator \
must review and activate drafts themselves in the Algorithms view.
- Be a straight-talking risk-aware desk partner: concise, concrete, numbers cited from tools \
with their source and time. Flag risk honestly. No hype, no financial-advice hedging \
boilerplate — the operator knows this is their own platform.
- Formatting: plain text with short paragraphs and simple dashes for lists. No markdown \
tables or headers — the chat panel renders plain text.

A <session_context> block accompanies each operator message with the platform's current \
state (mode, market session, equity, positions). Trust it over memory."""


class ChatService:
    """One persisted conversation between the operator and Claude."""

    def __init__(self, config: AIConfig, backend: ChatBackend,
                 dispatcher: ToolDispatcher, db: Database) -> None:
        self._config = config
        self._backend = backend
        self._dispatcher = dispatcher
        self._db = db
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    # -- public API -------------------------------------------------------------

    async def send(self, message: str, *, context: str) -> dict[str, Any]:
        """Process one operator message. Returns {reply, tool_calls, usage}.

        Raises ChatBusyError when a previous message is still in flight and
        AgentError on API failures (the operator's message is still saved so
        nothing typed is ever lost).
        """
        message = message.strip()[:_MAX_MESSAGE_CHARS]
        if not message:
            raise ValueError("empty message")
        if self._lock.locked():
            raise ChatBusyError("Claude is still answering the previous message")
        # The context block is the platform's word, not the operator's — a
        # message must not be able to forge or close one (any case/spacing).
        message = _SESSION_CONTEXT_TAG.sub(lambda m: f"[{m.group(1)}session_context]", message)
        async with self._lock:
            history = await self._history_as_messages(_HISTORY_TURNS)
            await self._persist("user", message)
            current = f"<session_context>\n{context}\n</session_context>\n\n{message}"
            messages: list[dict[str, Any]] = [*history, {"role": "user", "content": current}]

            usage = {"input_tokens": 0, "output_tokens": 0,
                     "cache_read_tokens": 0, "cache_write_tokens": 0, "api_calls": 0}
            tool_calls: list[str] = []
            reply = ""
            try:
                reply = await self._run_tool_loop(messages, usage, tool_calls)
            except AgentError as exc:
                # Keep the history PAIRED: without an assistant turn the
                # dangling user message would silently merge into the next
                # send. The marker also tells the operator what happened.
                await self._persist("assistant", f"(request failed: {exc})")
                # Carry partial usage (earlier tool-loop calls were billed) so
                # the caller can still meter it against the monthly budget.
                exc.usage = dict(usage)  # type: ignore[attr-defined]
                raise
            if not reply:
                reply = "(no response)"
            await self._persist("assistant", reply)
            return {"reply": reply, "tool_calls": tool_calls, "usage": usage}

    async def _run_tool_loop(self, messages: list[dict[str, Any]],
                             usage: dict[str, int], tool_calls: list[str]) -> str:
        for _ in range(self._config.max_tool_iterations):
            resp = await self._backend.complete(messages, tools=DATA_TOOLS, system=CHAT_SYSTEM_PROMPT)
            self._record_usage(resp.usage, usage)
            if resp.stop_reason == "refusal":
                return "I can't help with that request."
            messages.append(resp.assistant_message)
            if resp.stop_reason == "pause":
                continue
            if not resp.tool_calls:
                return resp.text.strip()
            results: list[ToolResult] = []
            for tc in resp.tool_calls:
                out, is_error = await self._dispatcher.dispatch(tc.name, tc.input)
                log.info("chat tool call", tool=tc.name, error=is_error)
                tool_calls.append(tc.name)
                results.append(ToolResult(tc.id, out, is_error))
            messages.extend(self._backend.tool_result_messages(results))
        return "I hit the tool-call limit before finishing — ask me to continue."

    async def history(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT role, content, created_at FROM chat_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [{"role": r[0], "content": r[1], "at": r[2]} for r in reversed(rows)]

    async def clear(self) -> None:
        await self._db.execute("DELETE FROM chat_messages")

    # -- internals --------------------------------------------------------------

    async def _history_as_messages(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT role, content FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)
        )
        ordered = list(reversed(rows))
        # The API requires the first message to be a user turn.
        while ordered and ordered[0][0] != "user":
            ordered.pop(0)
        return [{"role": r[0], "content": r[1]} for r in ordered]

    async def _persist(self, role: str, content: str) -> None:
        await self._db.execute(
            "INSERT INTO chat_messages (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def _record_usage(usage_in: dict[str, int], usage: dict[str, int]) -> None:
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            usage[k] += usage_in.get(k, 0)
        usage["api_calls"] += 1
