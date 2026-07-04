"""Unit tests for the Dry Run state summarizer (GET /api/dryrun)."""

from __future__ import annotations

from poseidon.api.server import build_dryrun_state
from poseidon.core.enums import MarketSession
from poseidon.strategy.workshop import BUNDLED_REVIEW_NOTE


def _algo(id_: str, status: str, *, bundled: bool = False) -> dict:
    return {"id": id_, "name": f"algo-{id_}", "status": status,
            "review_notes": BUNDLED_REVIEW_NOTE if bundled else ""}


def test_dryrun_state_counts_and_flags() -> None:
    state = build_dryrun_state(
        broker_is_paper=True, active_broker="paper", mode_value="research",
        algorithms_raw=[_algo("a", "active", bundled=True),
                        _algo("b", "draft", bundled=True),
                        _algo("c", "draft", bundled=False)],
        session=MarketSession.REGULAR,
    )
    assert state["broker_is_paper"] is True
    assert state["active_broker"] == "paper"
    assert state["mode"] == "research"
    assert state["active_algo_count"] == 1
    assert state["bundled_draft_count"] == 1  # 'b' only (c is not bundled)
    assert state["market"] == {"session": "regular", "is_open": True, "opens_hint": None}
    ids = {a["id"]: a for a in state["algorithms"]}
    assert ids["a"]["bundled"] is True and ids["c"]["bundled"] is False


def test_dryrun_state_market_closed() -> None:
    state = build_dryrun_state(
        broker_is_paper=False, active_broker="alpaca", mode_value="autonomous",
        algorithms_raw=[], session=MarketSession.CLOSED,
    )
    assert state["market"] == {"session": "closed", "is_open": False, "opens_hint": "9:30 ET"}
    assert state["algorithms"] == [] and state["active_algo_count"] == 0
