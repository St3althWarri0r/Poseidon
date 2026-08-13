"""Published-strategy menu (r3 rank 4).

The catalogue's value is the feasibility SCREEN, not the list. These tests pin
the properties that make it a work queue rather than a reading list: every
entry has a verdict, every blocked entry names its blocker, and the reported
figures are never presented as Poseidon's own results.
"""

from __future__ import annotations

import json
from pathlib import Path

import poseidon.research.menu as menu_module
from poseidon.research.menu import (
    blocked_reasons,
    feasible_ideas,
    ideas_requiring,
    load_menu,
    render,
)


def test_menu_loads_and_is_non_trivial() -> None:
    ideas = load_menu()
    assert len(ideas) >= 50
    assert all(idea.title and idea.paper for idea in ideas)


def test_sorted_by_reported_sharpe_descending() -> None:
    sharpes = [idea.sharpe for idea in load_menu()]
    assert sharpes == sorted(sharpes, reverse=True)


def test_every_idea_carries_a_verdict_and_a_capability_list() -> None:
    for idea in load_menu():
        assert idea.feasible in {"yes", "partial", "no"}
        assert idea.requires, f"{idea.title} declares no capabilities"
        assert idea.note, f"{idea.title} has no rationale"


def test_every_blocked_idea_names_its_blocker() -> None:
    # An exclusion without a reason is indistinguishable from an oversight.
    reasons = blocked_reasons()
    assert reasons
    assert all(len(reason) > 20 for reason in reasons.values())


def test_feasible_subset_excludes_blocked_and_respects_include_partial() -> None:
    everything = load_menu()
    with_partial = feasible_ideas(include_partial=True)
    strict = feasible_ideas(include_partial=False)
    assert all(idea.feasible != "no" for idea in with_partial)
    assert all(idea.feasible == "yes" for idea in strict)
    assert len(strict) <= len(with_partial) < len(everything)


def test_capability_query_answers_what_a_feature_unlocks() -> None:
    unlocked = ideas_requiring("fundamentals")
    assert unlocked
    assert all("fundamentals" in idea.requires for idea in unlocked)
    assert all(idea.feasible != "no" for idea in unlocked)
    assert ideas_requiring("no_such_capability") == ()


def test_capability_query_is_case_insensitive() -> None:
    assert ideas_requiring("BARS") == ideas_requiring("bars")


def test_no_strategy_claims_capabilities_poseidon_lacks() -> None:
    # A feasible idea must only require capabilities that actually exist —
    # otherwise the screen is decorative.
    from poseidon.data.base import DataCapability

    known = {c.value for c in DataCapability} | {"profile", "sector"}
    for idea in feasible_ideas():
        unknown = set(idea.requires) - known
        assert not unknown, f"{idea.title} requires unknown {unknown}"


def test_render_labels_the_figures_as_the_papers_own() -> None:
    text = render()
    assert "SOURCE PAPERS" in text
    assert "not Poseidon backtests" in text.lower() or "not poseidon" in text.lower()


def test_bundled_file_records_provenance_and_licensing() -> None:
    raw = json.loads(
        (Path(menu_module.__file__).parent / "data" / "strategy_menu.json")
        .read_text(encoding="utf-8"))
    provenance = raw["provenance"].lower()
    # The source index is unlicensed, so the file must state that nothing was
    # copied from it — this is a licensing claim, and it has to be checkable.
    assert "no code" in provenance
    assert "awesome-systematic-trading" in provenance


def test_load_is_cached() -> None:
    assert load_menu() is load_menu()
