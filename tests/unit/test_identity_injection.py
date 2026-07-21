"""Task 6 (verified-snapshot design §3.5): resolved instrument identities reach
the PM ONLY through the per-cycle user turn — the cache-controlled system
prompts stay byte-identical to v2.12.1 — and the app helper that resolves them
fails open per symbol so it can never block or fail a review cycle.
"""
from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime

from poseidon.ai.agent import SYSTEM_PROMPT, ClaudeAgent
from poseidon.ai.backends.base import ToolCall
from poseidon.ai.chat import CHAT_SYSTEM_PROMPT
from poseidon.core.enums import TradingMode
from poseidon.core.models import InstrumentProfile

from .backend_fakes import FakeBackend, tool_use


class _Dispatcher:
    def __init__(self) -> None:
        self.sources_used: set[str] = set()

    def reset_cycle_budget(self) -> None:
        pass

    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        return ('{"ok": true}', False)


def _prompt(identities: dict[str, str] | None) -> str:
    return ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL", "MSFT"], enabled_strategies=[], strategy_signals=[],
        market_session="open", instrument_identities=identities)


# ------------------------------------------------------------- prompt render


async def test_identity_block_rendered_in_user_text() -> None:
    identities = {
        "MSFT": "Microsoft Corp (NASDAQ NMS - GLOBAL MARKET, equity)",
        "AAPL": "Apple Inc (NASDAQ NMS - GLOBAL MARKET, equity)",
    }
    prompt = _prompt(identities)
    line = next(ln for ln in prompt.splitlines() if ln.startswith("Instrument identities"))
    # Sorted "; "-joined "SYM = desc" pairs, with the do-not-substitute rule
    # travelling with the identity block (never the system prompt).
    assert line.index("AAPL = Apple Inc") < line.index("MSFT = Microsoft Corp")
    assert "AAPL = Apple Inc (NASDAQ NMS - GLOBAL MARKET, equity); MSFT = " in line
    assert "analyze ONLY these instruments" in line
    assert "never infer its company from memory or substitute" in line
    # Placement: immediately after the Watchlist line (design §3.5).
    lines = prompt.splitlines()
    assert lines[lines.index(line) - 1].startswith("Watchlist:")

    # run_cycle threads the identities into what is actually sent to the model.
    from poseidon.core.config import AIConfig

    backend = FakeBackend([
        tool_use(ToolCall("d1", "submit_decision",
                          {"action": "no_action", "trades": [], "summary": "flat"})),
    ])
    agent = ClaudeAgent(AIConfig(), backend, _Dispatcher())  # type: ignore[arg-type]
    await agent.run_cycle(
        mode=TradingMode.RESEARCH, watchlist=["AAPL"], enabled_strategies=[],
        strategy_signals=[], market_session="regular", instrument_identities=identities)
    sent = backend.calls[0]["messages"][0]["content"]
    assert "AAPL = Apple Inc (NASDAQ NMS - GLOBAL MARKET, equity)" in sent


def test_no_block_when_empty_or_none() -> None:
    assert "Instrument identities" not in _prompt(None)
    assert "Instrument identities" not in _prompt({})
    # Default: callers that don't pass the kwarg are unchanged.
    sig = inspect.signature(ClaudeAgent._cycle_prompt)
    assert sig.parameters["instrument_identities"].default is None
    assert inspect.signature(ClaudeAgent.run_cycle).parameters[
        "instrument_identities"].default is None


def test_system_prompt_byte_identical() -> None:
    # The Anthropic backend cache-controls tools+system as the frozen prefix;
    # identity text must ride the user turn only. Hashes pin the current prompt
    # bytes (last updated for the position-sizing/risk-case section) — any
    # drift busts the prompt cache and fails here until consciously re-pinned.
    assert hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest() == (
        "bb906e82e5f209633bf0b5f42c7c3d1eacc6bf88a195051b55dac0e595dc73b6")
    assert hashlib.sha256(CHAT_SYSTEM_PROMPT.encode()).hexdigest() == (
        "324ff9971e78a1aba9b6dc7d90e6e57c6125d67bc767a2926f2069ab73c670c1")
    assert "Instrument identities" not in SYSTEM_PROMPT
    assert "Instrument identities" not in CHAT_SYSTEM_PROMPT


# ------------------------------------------------------------------ app helper


def _profile(symbol: str, name: str, exchange: str | None = "NASDAQ") -> InstrumentProfile:
    return InstrumentProfile(symbol=symbol, name=name, exchange=exchange,
                             currency="USD", as_of=datetime.now(UTC), source="finnhub")


class _FakeRouter:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self._outcomes = outcomes
        self.calls: list[str] = []

    async def profile(self, symbol: str) -> InstrumentProfile | None:
        self.calls.append(symbol)
        out = self._outcomes[symbol]
        if isinstance(out, Exception):
            raise out
        assert out is None or isinstance(out, InstrumentProfile)
        return out


def _kernel(router: _FakeRouter, *, identity: bool = True):
    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AIConfig, AppConfig, SnapshotConfig

    cfg = AppConfig(ai=AIConfig(snapshot=SnapshotConfig(identity=identity)))
    kernel = ApplicationKernel.__new__(ApplicationKernel)
    kernel.config = cfg
    kernel.router = router  # type: ignore[assignment]
    return kernel


async def test_app_helper_fails_open_per_symbol() -> None:
    router = _FakeRouter({
        "AAPL": _profile("AAPL", "Apple Inc"),
        "BOOM": RuntimeError("provider exploded"),
        "ETF": None,  # unresolved (router fail-open) — omitted, never guessed
    })
    kernel = _kernel(router)
    identities = await kernel._instrument_identities(["AAPL", "BOOM", "ETF"])
    assert identities == {"AAPL": "Apple Inc (NASDAQ, equity)"}
    assert router.calls == ["AAPL", "BOOM", "ETF"]  # one failure never stops the rest


async def test_helper_disabled_by_config_flag() -> None:
    router = _FakeRouter({"AAPL": _profile("AAPL", "Apple Inc")})
    kernel = _kernel(router, identity=False)
    assert await kernel._instrument_identities(["AAPL"]) == {}
    assert router.calls == []  # no lookups at all when ai.snapshot.identity is off
