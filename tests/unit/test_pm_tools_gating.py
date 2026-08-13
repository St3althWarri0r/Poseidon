"""Invariant pins for the pm_tools catalog gating (rank 6).

The ship-OFF invariant, byte-exact: with every pm_tools flag at its default
the agent catalog equals ``ALL_TOOLS`` and the chat catalog equals
``DATA_TOOLS`` EXACTLY (rank 4's object-identity pins stay green through the
merged composition). Enabled flags append the trio in fixed order,
``submit_decision`` stays last-and-exactly-once on the agent path and NEVER
appears on chat (invariant 8), every new schema is strict, and the budget
gate's membership is the exact 13-name contract.
"""

from __future__ import annotations

from poseidon.ai.agent import ClaudeAgent
from poseidon.ai.chat import ChatService
from poseidon.ai.schemas import (
    ALL_TOOLS,
    CORRELATION_TOOL,
    DATA_TOOLS,
    FUNDAMENTALS_TOOLS,
    MACRO_CONTEXT_TOOL,
    READ_URL_TOOL,
    SCREEN_MARKET_TOOL,
    SUBMIT_DECISION_TOOL,
    optional_data_tools,
)
from poseidon.ai.tools import _DATA_TOOL_NAMES
from poseidon.core.config import (
    AIConfig,
    FundamentalsConfig,
    PMToolsConfig,
    WebReadConfig,
)

_TRIO_NAMES = ["read_url", "screen_market", "compute_correlation_matrix"]


def _agent(cfg: AIConfig) -> ClaudeAgent:
    return ClaudeAgent(cfg, None, None)  # type: ignore[arg-type]


def _chat(cfg: AIConfig) -> ChatService:
    return ChatService(cfg, None, None, None)  # type: ignore[arg-type]


def _all_on(**overrides: object) -> AIConfig:
    return AIConfig(pm_tools=PMToolsConfig(
        web_read=WebReadConfig(enabled=True),
        screen_market=True, correlation=True), **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------- off == today


def test_defaults_are_all_off() -> None:
    pm = PMToolsConfig()
    assert pm.web_read.enabled is False
    assert pm.web_read.allow_http is False
    assert pm.screen_market is False
    assert pm.correlation is False
    assert pm.macro_context is False
    assert optional_data_tools(pm) == []


def test_disabled_default_catalogs_equal_module_constants_exactly() -> None:
    cfg = AIConfig()
    assert _agent(cfg)._tools == ALL_TOOLS  # list equality: byte-identical off state
    assert _chat(cfg)._tools == DATA_TOOLS
    # …and rank 4's stronger object-identity contract survives the merge.
    assert _agent(cfg)._tools is ALL_TOOLS
    assert _chat(cfg)._tools is DATA_TOOLS


def test_new_tools_never_leak_into_module_constants() -> None:
    for tools in (DATA_TOOLS, ALL_TOOLS):
        assert not ({t["name"] for t in tools} & set(_TRIO_NAMES))


# ------------------------------------------------------------ enabled catalogs


def test_all_flags_on_appends_trio_in_fixed_order() -> None:
    cfg = _all_on()
    names = [t["name"] for t in _agent(cfg)._tools]
    assert names == [*(t["name"] for t in DATA_TOOLS), *_TRIO_NAMES, "submit_decision"]
    assert names.count("submit_decision") == 1 and names[-1] == "submit_decision"
    chat_names = [t["name"] for t in _chat(cfg)._tools]
    assert chat_names == [*(t["name"] for t in DATA_TOOLS), *_TRIO_NAMES]
    assert "submit_decision" not in chat_names  # invariant 8: chat cannot trade


def test_single_flag_enables_single_tool() -> None:
    assert optional_data_tools(PMToolsConfig(
        web_read=WebReadConfig(enabled=True))) == [READ_URL_TOOL]
    assert optional_data_tools(PMToolsConfig(screen_market=True)) == [SCREEN_MARKET_TOOL]
    assert optional_data_tools(PMToolsConfig(correlation=True)) == [CORRELATION_TOOL]
    assert optional_data_tools(PMToolsConfig(
        web_read=WebReadConfig(enabled=True), screen_market=True,
        correlation=True)) == [READ_URL_TOOL, SCREEN_MARKET_TOOL, CORRELATION_TOOL]


def test_combined_with_fundamentals_keeps_contracted_order() -> None:
    # The one composition no single rank's suite would otherwise cover: both
    # gates on -> DATA, FUNDAMENTALS, pm trio, submit_decision last.
    cfg = _all_on(fundamentals=FundamentalsConfig(enabled=True))
    assert _agent(cfg)._tools == [*DATA_TOOLS, *FUNDAMENTALS_TOOLS, READ_URL_TOOL,
                                  SCREEN_MARKET_TOOL, CORRELATION_TOOL,
                                  SUBMIT_DECISION_TOOL]
    assert _chat(cfg)._tools == [*DATA_TOOLS, *FUNDAMENTALS_TOOLS, READ_URL_TOOL,
                                 SCREEN_MARKET_TOOL, CORRELATION_TOOL]


def test_fundamentals_only_unaffected_by_pm_tools_default() -> None:
    cfg = AIConfig(fundamentals=FundamentalsConfig(enabled=True))
    assert _agent(cfg)._tools == [*DATA_TOOLS, *FUNDAMENTALS_TOOLS, SUBMIT_DECISION_TOOL]
    assert _chat(cfg)._tools == [*DATA_TOOLS, *FUNDAMENTALS_TOOLS]


# ------------------------------------------------------------- schema hygiene


def test_trio_schemas_are_strict_and_fully_required() -> None:
    for tool in (READ_URL_TOOL, SCREEN_MARKET_TOOL, CORRELATION_TOOL):
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == sorted(schema["properties"])
    assert READ_URL_TOOL["input_schema"]["properties"]["offset"]["minimum"] == 0
    assert SCREEN_MARKET_TOOL["input_schema"]["properties"]["universe"]["enum"] == [
        "sp500", "crypto"]
    symbols = CORRELATION_TOOL["input_schema"]["properties"]["symbols"]
    assert symbols["minItems"] == 2 and symbols["maxItems"] == 30
    # Untrusted-data + never-a-price-source framing rides the description
    # (SYSTEM_PROMPT is sha-pinned and must not change).
    desc = READ_URL_TOOL["description"]
    assert "UNTRUSTED" in desc and "never" in desc.lower() and "price" in desc.lower()
    assert "never a trade signal" in SCREEN_MARKET_TOOL["description"]
    assert "advisory" in CORRELATION_TOOL["description"].lower()


def test_budget_gate_membership_is_the_fourteen_name_contract() -> None:
    contracted = {
        "get_quote", "get_bars", "get_option_chain", "get_news",
        "get_earnings_calendar", "get_economic_calendar", "get_market_snapshot",
        "get_fundamentals", "get_filings", "get_insider_transactions",
        "read_url", "screen_market", "compute_correlation_matrix",
        # get_macro_context fetches over the network like the rest, so it sits
        # under the same per-cycle ceiling rather than beside it.
        "get_macro_context",
    }
    assert set(_DATA_TOOL_NAMES) == contracted
    assert len(contracted) == 14


# ------------------------------------------------------- macro context (r3)


def test_macro_context_is_off_by_default_and_absent_from_the_catalog() -> None:
    pm = PMToolsConfig()
    assert pm.macro_context is False
    assert MACRO_CONTEXT_TOOL not in optional_data_tools(pm)


def test_macro_context_appears_last_when_enabled() -> None:
    pm = PMToolsConfig(web_read=WebReadConfig(enabled=True), screen_market=True,
                       correlation=True, macro_context=True)
    names = [t["name"] for t in optional_data_tools(pm)]
    assert names == [*_TRIO_NAMES, "get_macro_context"]


def test_macro_context_can_be_enabled_alone() -> None:
    pm = PMToolsConfig(macro_context=True)
    assert [t["name"] for t in optional_data_tools(pm)] == ["get_macro_context"]


def test_macro_tool_disclaims_price_truth_in_its_own_description() -> None:
    # The model reads the catalog before it ever calls the tool, so the
    # "not a price source" contract has to be stated HERE too, not only in
    # the payload it gets back afterwards.
    description = MACRO_CONTEXT_TOOL["description"].lower()
    assert "delayed" in description
    assert "get_quote" in description
    assert "never a price source" in description or "not a price source" in description


async def test_disabled_macro_tool_raises_rather_than_silently_returning() -> None:
    # dispatch() resolves any _tool_* by getattr, so catalog absence alone is
    # not a gate: the handler must refuse on its own.
    import json

    from poseidon.ai.tools import ToolDispatcher

    disp = ToolDispatcher(None, None, None,  # type: ignore[arg-type]
                          allow_delayed_quotes=True, pm_tools=PMToolsConfig())
    out, is_error = await disp.dispatch("get_macro_context", {})
    assert is_error is True
    assert "disabled in config" in json.loads(out)["error"]
