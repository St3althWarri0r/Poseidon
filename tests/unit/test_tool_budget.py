"""TASK 2 (whole-market design §Part 1(b)/(c)): bound the raw data the PM's tool
loop can pour into the context window.

``ToolDispatcher`` now takes a ``CycleBudgetConfig`` and enforces three layers:
  * per-tool count caps — ``get_bars`` returns at most ``max_bars_returned`` bars
    (newest kept, with an explicit "capped" note); ``get_news`` returns at most
    ``max_news_articles`` and truncates each summary to ``max_news_summary_chars``;
  * a per-result size cap — ``max_tool_result_chars`` (replacing the old 60_000
    constant), still via the envelope in ``_truncate`` (never a mid-token price);
  * a per-cycle cumulative counter — over ``soft_cycle_tool_chars`` a converge
    nudge is attached (real data still returned); over ``hard_cycle_tool_chars``
    further data tools return a compact "budget reached" envelope.
``reset_cycle_budget()`` zeroes the counter at the start of each cycle.

No omission is silent, no price is cut mid-value, and the anti-confabulation
snapshot surface is untouched (covered in test_tool_market_snapshot.py).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.ai.tools import ToolDispatcher
from poseidon.core.config import CycleBudgetConfig
from poseidon.core.models import Bar, NewsArticle

_AS_OF = datetime(2026, 7, 16, 15, 30, tzinfo=UTC)


def _bars(n: int) -> list[Bar]:
    out: list[Bar] = []
    day0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        close = Decimal("100.00") + Decimal(i) * Decimal("0.25")
        start = day0 + timedelta(days=i)
        out.append(Bar(symbol="AAPL", open=close - Decimal("0.50"),
                       high=close + Decimal("1.00"), low=close - Decimal("1.00"),
                       close=close, volume=1000 + i, start=start,
                       end=start + timedelta(days=1), source="barsrc"))
    return out


def _news(n: int, *, summary_len: int = 400) -> list[NewsArticle]:
    return [
        NewsArticle(headline=f"Headline {i}", summary="z" * summary_len,
                    published_at=_AS_OF, source="newsrc", symbols=["AAPL"])
        for i in range(n)
    ]


class _Router:
    def __init__(self, bars: list[Bar] | None = None,
                 news: list[NewsArticle] | None = None) -> None:
        self._bars = bars if bars is not None else []
        self._news = news if news is not None else []

    async def bars(self, symbol: str, timeframe: str = "1d", limit: int = 250) -> list[Bar]:
        return self._bars

    async def news(self, symbols: list[str] | None = None, *, limit: int = 25) -> list[NewsArticle]:
        return self._news


def _disp(router: _Router, budget: CycleBudgetConfig | None = None) -> ToolDispatcher:
    return ToolDispatcher(router, None, None,  # type: ignore[arg-type]
                          allow_delayed_quotes=True, budget=budget)


# ---------------------------------------------------------------- config wiring


def test_dispatcher_defaults_budget_when_none() -> None:
    disp = _disp(_Router())
    assert isinstance(disp._budget, CycleBudgetConfig)
    assert disp._budget.max_tool_result_chars == 12000


def test_reset_cycle_budget_zeroes_counter() -> None:
    disp = _disp(_Router())
    disp._cycle_tool_chars = 5000
    disp.reset_cycle_budget()
    assert disp._cycle_tool_chars == 0


# ------------------------------------------------------------- get_bars capping


async def test_get_bars_capped_to_max_with_note_keeps_newest() -> None:
    full = _bars(300)
    disp = _disp(_Router(bars=full), CycleBudgetConfig(max_bars_returned=120))
    result = await disp._tool_get_bars("AAPL", "1d", 300)
    assert len(result["bars"]) == 120
    assert "note" in result and "capped" in result["note"].lower()
    # NEWEST kept: the last returned bar is the last (newest) source bar, and the
    # first returned bar is exactly full[-120] — never a stale head of the series.
    assert result["bars"][-1]["close"] == str(full[-1].close)
    assert result["bars"][0]["close"] == str(full[-120].close)


async def test_get_bars_no_note_when_within_cap() -> None:
    disp = _disp(_Router(bars=_bars(50)), CycleBudgetConfig(max_bars_returned=120))
    result = await disp._tool_get_bars("AAPL", "1d", 50)
    assert len(result["bars"]) == 50
    assert "note" not in result


# ------------------------------------------------------------- get_news capping


async def test_get_news_capped_and_summary_truncated() -> None:
    disp = _disp(_Router(news=_news(30, summary_len=400)),
                 CycleBudgetConfig(max_news_articles=10, max_news_summary_chars=50))
    result = await disp._tool_get_news(["AAPL"], 30)
    assert len(result["articles"]) == 10
    for item in result["articles"]:
        summary = item["summary"]
        assert len(summary) <= 51  # 50 chars + one ellipsis marker
        assert summary.endswith("…")


async def test_get_news_short_summary_untouched() -> None:
    disp = _disp(_Router(news=_news(3, summary_len=10)),
                 CycleBudgetConfig(max_news_articles=10, max_news_summary_chars=50))
    result = await disp._tool_get_news(["AAPL"], 3)
    assert len(result["articles"]) == 3
    assert result["articles"][0]["summary"] == "z" * 10  # verbatim, no ellipsis


# ----------------------------------------------------- per-result char envelope


async def test_per_result_capped_to_max_tool_result_chars() -> None:
    # 200 bars, a small per-result cap: the payload is far over the cap, so
    # dispatch returns the truncation envelope (never a mid-token price slice).
    disp = _disp(_Router(bars=_bars(200)),
                 CycleBudgetConfig(max_bars_returned=200, max_tool_result_chars=1000))
    payload, is_error = await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 200})
    assert is_error is False
    parsed = json.loads(payload)
    assert parsed.get("truncated") is True


# -------------------------------------------------- cumulative soft/hard budget


async def test_dispatch_increments_cumulative_counter() -> None:
    disp = _disp(_Router(bars=_bars(30)))
    assert disp._cycle_tool_chars == 0
    await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    assert disp._cycle_tool_chars > 0


async def test_soft_budget_attaches_converge_nudge_but_returns_real_data() -> None:
    # A single 60-bar payload is several KB, so one dispatch pushes the counter
    # past a 1000-char soft budget; the *next* call then carries the nudge.
    disp = _disp(_Router(bars=_bars(60)),
                 CycleBudgetConfig(soft_cycle_tool_chars=1000,
                                   hard_cycle_tool_chars=10_000_000))
    first, _ = await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    assert "budget_note" not in json.loads(first)  # under soft on the first call
    second, is_error = await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    parsed = json.loads(second)
    assert is_error is False
    assert "budget_note" in parsed          # nudge attached
    assert parsed["bars"]                    # real data STILL returned


async def test_hard_budget_returns_compact_envelope_for_data_tools() -> None:
    disp = _disp(_Router(bars=_bars(60)),
                 CycleBudgetConfig(soft_cycle_tool_chars=1000,
                                   hard_cycle_tool_chars=2000))
    await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    payload, is_error = await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    parsed = json.loads(payload)
    assert is_error is False
    assert parsed.get("budget_exhausted") is True
    assert "bars" not in parsed  # no fresh raw data pulled once the ceiling is hit


async def test_reset_restores_full_data_after_hard_budget() -> None:
    disp = _disp(_Router(bars=_bars(60)),
                 CycleBudgetConfig(soft_cycle_tool_chars=1000, hard_cycle_tool_chars=2000))
    await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    exhausted, _ = await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    assert json.loads(exhausted).get("budget_exhausted") is True
    disp.reset_cycle_budget()
    fresh, _ = await disp.dispatch("get_bars", {"symbol": "AAPL", "timeframe": "1d", "limit": 30})
    assert json.loads(fresh)["bars"]  # data flows again after the reset
