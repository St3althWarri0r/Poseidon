"""Task 10 safety invariant: the analysis firm's packets reach the PM ONLY
through the review-cycle user-turn prompt, and only their ids (never packet
prose) land on the returned Decision. The packet/RiskLens objects must never
reach RiskEngine, OrderManager, the submit_decision tool schema, or the chat
dispatcher — see docs/superpowers/specs/2026-07-14-debate-packet-design.md
invariant #1.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime

from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.backends.base import ToolCall
from poseidon.core.enums import TradingMode
from poseidon.core.models import AnalysisPacket, AnalystReport, DebateVerdict, RiskLens

from .backend_fakes import FakeBackend, text_end, tool_use


def _packet(*, packet_id: str = "p1", symbol: str = "AAPL",
           synthesis: str = "firmsynth") -> AnalysisPacket:
    return AnalysisPacket(
        id=packet_id, symbol=symbol, as_of=datetime.now(UTC), model="m",
        reports=[AnalystReport(role="news", summary="s", stance="bullish", confidence=0.6,
                               key_points=[], data_gaps=[], sources=[])],
        verdict=DebateVerdict(direction="long", conviction=0.6, bull_case="b", bear_case="c",
                              synthesis=synthesis, rounds=1),
        risk_lens=RiskLens(aggressive="a", neutral="n", conservative="c", synthesis="s"),
        snapshot_digest="d",
    )


class _Dispatcher:
    def __init__(self) -> None:
        self.sources_used: set[str] = set()

    async def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        self.sources_used.add("fake")
        return ('{"ok": true}', False)


# --------------------------------------------------------------- brief tests


def test_cycle_prompt_accepts_packets_and_injects_only_into_user_text() -> None:
    # The packet reaches the model ONLY through the user-turn prompt string.
    sig = inspect.signature(ClaudeAgent._cycle_prompt)
    assert "analysis_packets" in sig.parameters
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[], strategy_signals=[],
        market_session="open", analysis_packets=[_packet()])
    assert "firmsynth" in prompt
    assert "ADVISORY" in prompt.upper()


def test_agent_run_cycle_has_analysis_packets_param() -> None:
    assert "analysis_packets" in inspect.signature(ClaudeAgent.run_cycle).parameters


# ------------------------------------------------ config-driven bounded render


def test_cycle_prompt_bounds_each_packet_to_the_configured_max_render_chars() -> None:
    # A long synthesis must be truncated per-packet to whatever bound the
    # caller supplies (run_cycle threads ai.analysis.max_render_chars here), so
    # a single oversized packet can never balloon the decision-model prompt.
    long_pkt = _packet(synthesis="head-marker " + ("x" * 5000) + " tail-marker")
    # render()'s fixed "SYMBOL: firm view ... analysts[...]. synthesis: " prefix
    # is 75 chars for this packet; 150 leaves room for "head-marker" to survive
    # while still truncating long before the 5000-char filler + "tail-marker".
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[], strategy_signals=[],
        market_session="open", analysis_packets=[long_pkt], max_render_chars=150)
    assert "head-marker" in prompt
    assert "tail-marker" not in prompt         # truncated away by the small bound
    assert "x" * 5000 not in prompt


def test_cycle_prompt_with_no_packets_has_no_analysis_block() -> None:
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[], strategy_signals=[],
        market_session="open")
    assert "Advisory research packets" not in prompt


# ------------------------------------------------------- run_cycle end to end
# The actual payoff: packets reach the live prompt sent to the backend, and
# their ids (never prose) land on the returned Decision's explainability trace.


async def test_run_cycle_records_informing_packet_ids_on_the_decision() -> None:
    from poseidon.core.config import AIConfig

    pkt = _packet(packet_id="pkt-xyz")
    responses = [
        tool_use(ToolCall("d1", "submit_decision",
                          {"action": "no_action", "trades": [], "summary": "flat"})),
    ]
    backend = FakeBackend(responses)
    agent = ClaudeAgent(AIConfig(), backend, _Dispatcher())  # type: ignore[arg-type]

    decision = await agent.run_cycle(
        mode=TradingMode.RESEARCH, watchlist=["AAPL"], enabled_strategies=[],
        strategy_signals=[], market_session="regular", analysis_packets=[pkt])

    # Explainability trace: the id only, never packet prose.
    assert decision.analysis_packet_ids == ["pkt-xyz"]
    # The packet's synthesis actually reached the model's user turn (not just
    # what _cycle_prompt can produce in isolation — what run_cycle actually sent).
    sent = backend.calls[0]["messages"][0]["content"]
    assert "firmsynth" in sent


async def test_run_cycle_without_packets_leaves_the_trace_empty() -> None:
    from poseidon.core.config import AIConfig

    backend = FakeBackend([text_end("nothing interesting")])
    agent = ClaudeAgent(AIConfig(), backend, _Dispatcher())  # type: ignore[arg-type]

    decision = await agent.run_cycle(
        mode=TradingMode.RESEARCH, watchlist=["AAPL"], enabled_strategies=[],
        strategy_signals=[], market_session="regular")

    assert decision.analysis_packet_ids == []


# --------------------------------------------- wiring: flow isolation (kernel)
# Mirrors tests/unit/test_backend_tiering.py::test_wire_ai_binds_each_role_to_the_right_tier


async def test_wire_ai_builds_analysis_on_the_utility_tier_and_chat_has_no_packet_access(
    tmp_path,
) -> None:
    from types import SimpleNamespace

    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AIConfig, AppConfig
    from poseidon.security.vault import Vault

    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    # AnalysisService (like ReflectionService) stores these at construction but
    # never calls them here.
    kernel.db = None  # type: ignore[assignment]
    kernel.router = None  # type: ignore[assignment]
    kernel.audit = SimpleNamespace(append=None)  # type: ignore[assignment]
    disp, chat_disp = object(), object()

    cfg = AIConfig(backend="openai_compatible", base_url="http://x/v1",
                   model="big", utility_model="small")
    kernel._wire_ai(cfg, disp, chat_disp)  # type: ignore[arg-type]

    assert kernel.analysis is not None
    assert kernel.analysis._get_backend() is kernel._utility_backend
    # Genuinely the weaker tier, not an accidental alias of the money path.
    assert kernel.analysis._get_backend() is not kernel._backend
    assert kernel.agent.backend is kernel._backend  # the PM stays on the primary

    # Provenance isolation: chat must expose no packet/analysis accessor at all
    # (mirrors how chat cannot read trade lessons either — see chat.py).
    chat_attrs = {a.lower() for a in dir(kernel.chat) if not a.startswith("__")}
    assert not any("packet" in a or "analysis" in a for a in chat_attrs)


# ------------------------------- constructive flow isolation (static, on source)
# The invariant: analysis_packets may be handed ONLY to the _cycle_prompt call
# (the decision-id trace consumes it via a comprehension, never a call argument).
# Walk run_cycle's own AST and assert no OTHER call receives it by name — this
# would catch a future edit that threads it into the dispatcher, the risk/order
# path, or _parse_decision/_no_action_decision.


def test_run_cycles_only_consumer_of_analysis_packets_is_cycle_prompt() -> None:
    assert "analysis_packets" in inspect.signature(ClaudeAgent.run_cycle).parameters

    src = textwrap.dedent(inspect.getsource(ClaudeAgent.run_cycle))
    tree = ast.parse(src)
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef)

    calls_with_ref: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                name = "<dynamic>"
            referenced = any(
                isinstance(a, ast.Name) and a.id == "analysis_packets" for a in node.args
            ) or any(
                isinstance(kw.value, ast.Name) and kw.value.id == "analysis_packets"
                for kw in node.keywords
            )
            if referenced:
                calls_with_ref.append(name)
            self.generic_visit(node)

    _Visitor().visit(func)
    # Passed to _cycle_prompt exactly once, and to nothing else — never to the
    # dispatcher, _parse_decision/_no_action_decision, or any risk/order call.
    assert calls_with_ref == ["_cycle_prompt"], (
        f"analysis_packets must be passed only to _cycle_prompt; calls seen: {calls_with_ref}")
