"""TASK 1 (whole-market design §Part 1): the per-cycle user turn must never let
the ``strategy_signals`` block overflow the model context.

``ClaudeAgent._bounded_signals`` keeps the highest-conviction signals (top-K by
``strength``, capped by ``max_signal_entries``), renders each compactly (dropping
nested / long ``evidence`` so a fat evidence dict cannot balloon the prompt), and
hard-truncates the serialized block to ``max_signals_chars`` on a whole-entry
boundary. Whenever anything is dropped it appends an explicit omission marker so
the model knows data was withheld — the omission is never silent, and the
highest-strength signals (the ones most likely to become trades) always survive.
"""
from __future__ import annotations

import inspect
import json

from poseidon.ai.agent import ClaudeAgent
from poseidon.core.config import CycleBudgetConfig
from poseidon.core.enums import TradingMode


def _signals(n: int, *, fat: bool = False) -> list[dict[str, object]]:
    """n signals with ascending strength so the ranking is unambiguous; each
    carries a fat, nested evidence dict when ``fat`` is set."""
    out: list[dict[str, object]] = []
    for i in range(n):
        evidence: dict[str, object] = {"rsi": 30 + i, "note": "short"}
        if fat:
            # nested + long-string junk that must be dropped by the compactor
            evidence["history"] = [{"t": j, "px": 100 + j} for j in range(50)]
            evidence["blurb"] = "x" * 2000
            evidence["nested"] = {"a": {"b": {"c": list(range(100))}}}
        out.append({
            "strategy": "momo",
            "symbol": f"SYM{i:03d}",
            "direction": "long",
            "strength": round(0.001 * i, 3),  # SYM(n-1) is the strongest
            "evidence": evidence,
        })
    return out


# ------------------------------------------------------------- _bounded_signals


def test_empty_signals_render_as_none_unchanged() -> None:
    assert ClaudeAgent._bounded_signals([], CycleBudgetConfig()) == "none"


def test_block_never_exceeds_max_signals_chars() -> None:
    cfg = CycleBudgetConfig()
    block = ClaudeAgent._bounded_signals(_signals(200, fat=True), cfg)
    assert len(block) <= cfg.max_signals_chars


def test_only_top_k_by_strength_kept() -> None:
    cfg = CycleBudgetConfig(max_signal_entries=5, max_signals_chars=8000)
    block = ClaudeAgent._bounded_signals(_signals(40), cfg)
    # The 5 strongest are SYM035..SYM039; nothing weaker survives the cap.
    assert "SYM039" in block and "SYM035" in block
    assert "SYM034" not in block and "SYM000" not in block


def test_highest_strength_signal_always_survives() -> None:
    # Even at a punishing char budget the single strongest signal is kept.
    cfg = CycleBudgetConfig(max_signal_entries=40, max_signals_chars=400)
    block = ClaudeAgent._bounded_signals(_signals(200), cfg)
    assert "SYM199" in block  # strongest (strength 0.199)
    assert len(block) <= cfg.max_signals_chars


def test_omission_marker_present_and_counts_dropped_signals() -> None:
    cfg = CycleBudgetConfig(max_signal_entries=10, max_signals_chars=8000)
    block = ClaudeAgent._bounded_signals(_signals(200), cfg)
    assert "omitted" in block
    # 200 total, at most 10 kept -> at least 190 omitted; the marker states it.
    assert "190" in block


def test_no_marker_when_everything_fits() -> None:
    cfg = CycleBudgetConfig()
    block = ClaudeAgent._bounded_signals(_signals(3), cfg)
    assert "omitted" not in block
    # kept entries are still valid JSON-array-prefixed (compact, parseable head).
    assert block.startswith("[") and "SYM000" in block


def test_fat_evidence_is_dropped_but_scalar_evidence_kept() -> None:
    cfg = CycleBudgetConfig()
    block = ClaudeAgent._bounded_signals(_signals(3, fat=True), cfg)
    # The 2000-char blurb / nested history must not reach the prompt...
    assert "x" * 2000 not in block
    assert "history" not in block and "nested" not in block
    # ...but short scalar/string evidence survives.
    assert "rsi" in block and "short" in block


def test_kept_prefix_is_parseable_json_when_nothing_omitted() -> None:
    cfg = CycleBudgetConfig()
    block = ClaudeAgent._bounded_signals(_signals(2), cfg)
    parsed = json.loads(block)
    assert isinstance(parsed, list) and len(parsed) == 2
    assert {s["symbol"] for s in parsed} == {"SYM000", "SYM001"}


def test_bounded_signals_never_raises_on_missing_or_bad_strength() -> None:
    # A strategy dict without a strength (or a non-numeric one) must not crash
    # the prompt build — it sorts as weakest and still renders.
    weird = [
        {"symbol": "A", "direction": "long"},                 # no strength
        {"symbol": "B", "direction": "long", "strength": "?"},  # non-numeric
        {"symbol": "C", "direction": "long", "strength": 0.9},
    ]
    block = ClaudeAgent._bounded_signals(weird, CycleBudgetConfig())
    assert "C" in block  # the one real signal survives


# ------------------------------------------------------- wired into _cycle_prompt


def test_cycle_prompt_accepts_budget_param() -> None:
    assert "budget" in inspect.signature(ClaudeAgent._cycle_prompt).parameters


def test_cycle_prompt_bounds_signals_block() -> None:
    cfg = CycleBudgetConfig(max_signal_entries=10, max_signals_chars=1500)
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[],
        strategy_signals=_signals(200, fat=True),
        market_session="open", budget=cfg)
    # The huge fat-evidence junk never reaches the assembled prompt.
    assert "x" * 2000 not in prompt
    assert "omitted" in prompt
    # The strongest signal is still visible to the PM.
    assert "SYM199" in prompt


def test_cycle_prompt_signals_default_when_no_budget_given() -> None:
    # Backward-compatible: omitting budget uses CycleBudgetConfig() defaults and
    # small signal lists render verbatim (no marker).
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[],
        strategy_signals=_signals(2), market_session="open")
    assert "SYM000" in prompt and "omitted" not in prompt


def test_cycle_prompt_empty_signals_still_says_none() -> None:
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[], strategy_signals=[],
        market_session="open")
    assert "signals this cycle" in prompt
    assert ": none" in prompt


# -------------------------------------------- screener candidate block (TASK 6)


def test_cycle_prompt_accepts_screener_candidates_param() -> None:
    assert "screener_candidates" in inspect.signature(
        ClaudeAgent._cycle_prompt).parameters


def test_cycle_prompt_renders_screener_candidate_block() -> None:
    lines = ["BTC/USD score=+0.12 r1m=+0.08 r3m=+0.19 adv$=1.2B",
             "AAPL score=+0.05 r1m=+0.03 r3m=+0.07 adv$=8.0B"]
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[], strategy_signals=[],
        market_session="open", screener_candidates=lines)
    assert "Screener candidates" in prompt
    for line in lines:
        assert line in prompt  # each candidate's screen rationale is present in full


def test_cycle_prompt_candidate_block_always_full_regardless_of_budget() -> None:
    # Anti-starvation guarantee: even with a punishing signals budget AND fat
    # signal junk, every candidate line survives in full (the block is never
    # subject to the signal / tool caps — the PM is never blind to a candidate).
    cfg = CycleBudgetConfig(max_signal_entries=1, max_signals_chars=200)
    lines = [f"SYM{i:02d} score=+0.10 r1m=+0.05 r3m=+0.12 adv$=1.0M" for i in range(25)]
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=[], enabled_strategies=[],
        strategy_signals=_signals(200, fat=True),
        market_session="open", budget=cfg, screener_candidates=lines)
    for line in lines:
        assert line in prompt


def test_cycle_prompt_no_candidate_block_when_empty() -> None:
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[], strategy_signals=[],
        market_session="open", screener_candidates=[])
    assert "Screener candidates" not in prompt


def test_cycle_prompt_candidate_block_omitted_when_param_absent() -> None:
    # Backward-compatible: omitting the param yields no block (byte-identical to
    # the pre-dual-screener prompt for callers that never pass candidates).
    prompt = ClaudeAgent._cycle_prompt(
        cycle_id="c1", mode=TradingMode.RESEARCH,
        watchlist=["AAPL"], enabled_strategies=[], strategy_signals=[],
        market_session="open")
    assert "Screener candidates" not in prompt


# ----------------------------------------------------------- CycleBudgetConfig


def test_cycle_budget_config_defaults() -> None:
    c = CycleBudgetConfig()
    assert c.max_signal_entries == 40
    assert c.max_signals_chars == 8000
    assert c.max_prompt_chars == 16000
    assert c.max_bars_returned == 120
    assert c.max_news_articles == 10
    assert c.max_news_summary_chars == 500
    assert c.max_tool_result_chars == 12000
    assert c.soft_cycle_tool_chars == 40000
    assert c.hard_cycle_tool_chars == 64000


def test_ai_config_has_budget_defaulting_to_cycle_budget_config() -> None:
    from poseidon.core.config import AIConfig

    assert isinstance(AIConfig().budget, CycleBudgetConfig)
