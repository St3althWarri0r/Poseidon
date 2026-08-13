"""Published-strategy menu, screened against what Poseidon can actually trade.

A catalogue of systematic strategies from the academic literature, each with
the source paper's own reported Sharpe, volatility and rebalance cadence — and
crucially, a **feasibility verdict against Poseidon's real capabilities**.

That screen is the point. A list of 51 strategies is a reading list; a list of
the 23 you can actually run against the data you actually have, with the other
28 each carrying a named blocker, is a work queue. "Needs commodity futures" and
"needs filing text, and the filings capability serves metadata only by design"
are answers, not omissions.

Provenance and licensing: strategy names, reported metrics and paper URLs are
factual attributes of published work, gathered via the
``paperswithbacktest/awesome-systematic-trading`` index. **That repository
carries no license, so nothing was copied from it** — no strategy code, and no
reproduction of its table. The feasibility screen, the capability mapping and
this record structure are Poseidon's own.

Reported metrics are the PAPERS' figures, not Poseidon backtests. They are
research triage — a reason to test something, never evidence it works here.
Published backtests are optimistic essentially without exception: survivorship,
in-sample fitting, and costs that a live book actually pays.

Pure and offline: this module reads one bundled JSON file and performs no I/O
beyond it, so ``research/`` stays importable without the platform running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

_MENU_PATH = Path(__file__).parent / "data" / "strategy_menu.json"

Feasibility = Literal["yes", "partial", "no"]


@dataclass(frozen=True)
class StrategyIdea:
    """One published strategy and Poseidon's verdict on running it."""

    title: str
    sharpe: float
    volatility: float
    rebalance: str
    asset_class: str
    paper: str
    feasible: Feasibility
    requires: tuple[str, ...]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "reported_sharpe": self.sharpe,
            "reported_volatility": self.volatility,
            "rebalance": self.rebalance,
            "asset_class": self.asset_class,
            "paper": self.paper,
            "feasible": self.feasible,
            "requires": list(self.requires),
            "note": self.note,
        }


@lru_cache(maxsize=1)
def load_menu() -> tuple[StrategyIdea, ...]:
    """The full catalogue, best reported Sharpe first. Cached — it is a static
    bundled file, not live data."""
    raw = json.loads(_MENU_PATH.read_text(encoding="utf-8"))
    return tuple(
        StrategyIdea(
            title=row["title"], sharpe=float(row["sharpe"]),
            volatility=float(row["vol"]), rebalance=row["rebalance"],
            asset_class=row["asset_class"], paper=row["paper"],
            feasible=row["feasible"], requires=tuple(row["requires"]),
            note=row["note"],
        )
        for row in raw["strategies"]
    )


def feasible_ideas(*, include_partial: bool = True) -> tuple[StrategyIdea, ...]:
    """Only the ideas Poseidon can actually attempt, best Sharpe first."""
    allowed: set[str] = {"yes", "partial"} if include_partial else {"yes"}
    return tuple(idea for idea in load_menu() if idea.feasible in allowed)


def ideas_requiring(capability: str) -> tuple[StrategyIdea, ...]:
    """Feasible ideas that need a given capability.

    Answers the planning question directly: *what does turning on fundamentals
    actually unlock?*
    """
    key = capability.strip().lower()
    return tuple(idea for idea in feasible_ideas() if key in idea.requires)


def blocked_reasons() -> dict[str, str]:
    """Title -> why Poseidon cannot run it. Every exclusion is accounted for,
    so the catalogue can never quietly shrink."""
    return {idea.title: idea.note for idea in load_menu() if idea.feasible == "no"}


def render() -> str:
    """Operator-readable summary for the research CLI."""
    ideas = load_menu()
    counts = {verdict: sum(1 for i in ideas if i.feasible == verdict)
              for verdict in ("yes", "partial", "no")}
    lines = [
        f"Published strategy menu — {len(ideas)} candidates "
        f"({counts['yes']} runnable, {counts['partial']} partial, {counts['no']} blocked)",
        "Reported figures are the SOURCE PAPERS' own, not Poseidon backtests.",
        "",
        f"{'strategy':<52} {'Sharpe':>7} {'vol':>7} {'rebal':>10}  needs",
    ]
    for idea in feasible_ideas():
        mark = "" if idea.feasible == "yes" else " (partial)"
        lines.append(
            f"{idea.title[:52]:<52} {idea.sharpe:>7.3f} {idea.volatility:>6.1%} "
            f"{idea.rebalance:>10}  {','.join(idea.requires)}{mark}")
    return "\n".join(lines)
