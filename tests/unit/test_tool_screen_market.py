"""``screen_market`` tool pins (rank 6 red-first).

A thin, cache-first tool VIEW over the existing screener engines: flag off (the
default) is a disabled-in-config error envelope; flag on serves the engine's
ScoredCandidate rows with an advisory note; an engine that is disabled or has
no ranked screen yet returns an explicit note (``is_error`` False — a real
answer, not a crash); an unknown universe is an error envelope; and the tool
adds nothing to ``sources_used`` (risk-metrics precedent).
"""

from __future__ import annotations

import json

from poseidon.ai.tools import ToolDispatcher
from poseidon.core.config import PMToolsConfig
from poseidon.strategy.screener import ScoredCandidate


class _Screener:
    def __init__(self, candidates: list[ScoredCandidate]) -> None:
        self._candidates = candidates
        self.calls = 0

    async def ranked_candidates(self) -> list[ScoredCandidate]:
        self.calls += 1
        return list(self._candidates)


def _dispatcher(pm_tools: PMToolsConfig | None = None,
                screeners: dict[str, _Screener] | None = None) -> ToolDispatcher:
    return ToolDispatcher(object(), None, None,  # type: ignore[arg-type]
                          allow_delayed_quotes=True, pm_tools=pm_tools,
                          screeners=screeners)  # type: ignore[arg-type]


_CANDIDATES = [
    ScoredCandidate("NVDA", 0.42, 1.2e9, 0.30, 0.60),
    ScoredCandidate("AVGO", 0.31, 8.0e8, 0.25, 0.40),
]


async def test_flag_off_returns_error_envelope() -> None:
    screener = _Screener(_CANDIDATES)
    disp = _dispatcher(screeners={"sp500": screener})  # default config: off
    out, is_error = await disp.dispatch("screen_market", {"universe": "sp500"})
    assert is_error is True
    assert "disabled" in json.loads(out)["error"]
    assert screener.calls == 0  # the engine is never touched while gated off


async def test_enabled_serves_ranked_rows_with_advisory_note() -> None:
    disp = _dispatcher(PMToolsConfig(screen_market=True),
                       screeners={"sp500": _Screener(_CANDIDATES)})
    out, is_error = await disp.dispatch("screen_market", {"universe": "sp500"})
    assert is_error is False
    payload = json.loads(out)
    assert payload["universe"] == "sp500"
    assert payload["candidates"] == [
        {"symbol": "NVDA", "score": 0.42, "r_1m": 0.30, "r_3m": 0.60,
         "dollar_volume": 1.2e9},
        {"symbol": "AVGO", "score": 0.31, "r_1m": 0.25, "r_3m": 0.40,
         "dollar_volume": 8.0e8},
    ]
    assert "advisory" in payload["note"].lower()
    assert "never a trade signal" in payload["note"].lower()
    assert disp.sources_used == set()  # provenance precedent: risk metrics adds none


async def test_enabled_tool_with_disabled_engine_notes_it_honestly() -> None:
    # ranked_candidates() returns [] both when the engine is config-disabled
    # and when a screen degraded with an empty cache — a real answer either way.
    disp = _dispatcher(PMToolsConfig(screen_market=True),
                       screeners={"crypto": _Screener([])})
    out, is_error = await disp.dispatch("screen_market", {"universe": "crypto"})
    assert is_error is False
    payload = json.loads(out)
    assert payload["candidates"] == []
    assert "screener disabled or no ranked screen available" in payload["note"]
    assert "enable screener/crypto_screener in config" in payload["note"]


async def test_unknown_universe_is_error_envelope() -> None:
    disp = _dispatcher(PMToolsConfig(screen_market=True),
                       screeners={"sp500": _Screener(_CANDIDATES)})
    out, is_error = await disp.dispatch("screen_market", {"universe": "ftse"})
    assert is_error is True
    assert "ftse" in json.loads(out)["error"]


async def test_no_screeners_wired_degrades_to_error_envelope() -> None:
    # A dispatcher built without screeners (or before wiring) never crashes.
    disp = _dispatcher(PMToolsConfig(screen_market=True), screeners=None)
    out, is_error = await disp.dispatch("screen_market", {"universe": "sp500"})
    assert is_error is True
    assert "sp500" in json.loads(out)["error"]
