"""Integration (screener TASK 6): wire the screener into ``run_review_cycle``.

The cycle now feeds the AI ``watchlist ∪ candidates`` and forwards the screened
candidates to the strategies as ``extra_symbols`` — but ONLY when the screener is
enabled and a screen actually succeeds. This exercises the real
``ApplicationKernel.run_review_cycle`` over a real :class:`MarketScreener` and a
real :class:`DataRouter` fed by the in-memory ``FakeBatchProvider`` (no network);
a stub agent captures the ``watchlist`` the PM is handed, and a stub strategy
engine captures the ``extra_symbols`` it receives.

Safety invariants asserted here:
  * ENABLED ⇒ the top-N candidates are appended to the watchlist (union) and
    handed to the strategies as ``extra_symbols`` — the AI evaluates a wider set.
  * DISABLED ⇒ the agent sees the watchlist unchanged and ``extra_symbols=[]``
    (byte-identical to the pre-screener cycle).
  * A screen FAILURE degrades to the watchlist — the cycle still completes and
    never crashes; candidates are ``[]`` and the watchlist is untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime

from poseidon.app import ApplicationKernel
from poseidon.core.clock import FreshnessPolicy
from poseidon.core.config import AppConfig, ScreenerConfig, WatchlistConfig
from poseidon.core.enums import DecisionAction, TradingMode
from poseidon.core.models import Decision
from poseidon.data.router import DataRouter
from poseidon.security.audit import AuditLog
from poseidon.security.vault import Vault
from poseidon.storage.db import Database
from poseidon.strategy.screener import MarketScreener

from ..conftest import FakeBatchProvider


class _StubAgent:
    """Captures the ``watchlist`` the cycle hands the PM; returns a flat
    NO_ACTION decision so the cycle persists cleanly and never reaches the
    order path."""

    def __init__(self) -> None:
        self.captured_watchlist: list[str] | None = None
        self.calls = 0

    async def run_cycle(self, *, mode: object, watchlist: list[str],
                        enabled_strategies: object, strategy_signals: object,
                        market_session: object, market_regime: object = None,
                        trade_lessons: object = None, analysis_packets: object = None,
                        instrument_identities: object = None) -> Decision:
        self.captured_watchlist = list(watchlist)
        self.calls += 1
        return Decision(action=DecisionAction.NO_ACTION, trades=[],
                        cycle_id="itest", created_at=datetime.now(UTC))

    def last_cycle_usage(self) -> dict[str, int]:
        return {}


class _StubStrategies:
    """Captures the ``extra_symbols`` forwarded to ``scan_all``; emits no
    signals so the risk/order path stays inert."""

    def __init__(self) -> None:
        self.captured_extra: object = "unset"

    @property
    def enabled_names(self) -> list[str]:
        return []

    async def scan_all(self, router: object, portfolio: object, *,
                       extra_symbols: list[str] | None = None) -> list[object]:
        self.captured_extra = extra_symbols
        return []


class _StubRisk:
    def set_cycle_attribution(self, signals: object) -> None:
        pass


class _StubOrderManager:
    def __init__(self) -> None:
        self.mode = TradingMode.APPROVAL


async def _run_cycle(tmp_path, *, enabled: bool, watchlist: list[str],
                     fail_screen: bool = False,
                     provider: FakeBatchProvider | None = None) -> tuple[_StubAgent, _StubStrategies]:
    cfg = AppConfig(data_dir=tmp_path)
    cfg.watchlists = [WatchlistConfig(name="t", symbols=watchlist)]
    cfg.screener = ScreenerConfig(enabled=enabled)
    cfg.ai.snapshot.identity = False  # skip the per-symbol identity resolution loop

    kernel = ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))
    kernel.db = Database(tmp_path / "t.db")
    await kernel.db.open()
    kernel.audit = AuditLog(kernel.db)
    kernel.router = DataRouter([(provider or FakeBatchProvider(), 10)], FreshnessPolicy())
    kernel.risk = _StubRisk()  # type: ignore[assignment]
    strategies = _StubStrategies()
    kernel.strategies = strategies  # type: ignore[assignment]
    kernel.order_manager = _StubOrderManager()  # type: ignore[assignment]
    agent = _StubAgent()
    kernel.agent = agent  # type: ignore[assignment]
    kernel.screener = MarketScreener(cfg.screener, kernel.router)

    if fail_screen:
        async def _boom() -> list[object]:
            raise RuntimeError("simulated screen failure")
        kernel.screener._screen = _boom  # type: ignore[assignment]  # force the except branch

    try:
        await kernel.run_review_cycle()
    finally:
        await kernel.db.close()
        await kernel.bus.close()
    return agent, strategies


async def test_enabled_feeds_candidates_to_watchlist(tmp_path) -> None:
    watchlist = ["ZZZA", "ZZZB"]  # not S&P 500 names → disjoint from candidates
    agent, strategies = await _run_cycle(tmp_path, enabled=True, watchlist=watchlist)

    captured = agent.captured_watchlist
    assert captured is not None
    # The watchlist is preserved, in order, at the front of the union.
    assert captured[: len(watchlist)] == watchlist
    # Exactly top_n (=15) screened candidates were appended.
    candidates = captured[len(watchlist):]
    assert len(candidates) == ScreenerConfig().top_n == 15
    assert all(c not in watchlist for c in candidates)
    # The SAME candidates were forwarded to the strategies as extra_symbols.
    assert strategies.captured_extra == candidates


async def test_disabled_watchlist_only(tmp_path) -> None:
    watchlist = ["ZZZA", "ZZZB"]
    agent, strategies = await _run_cycle(tmp_path, enabled=False, watchlist=watchlist)

    # Byte-identical to the pre-screener cycle: watchlist unchanged, no candidates.
    assert agent.captured_watchlist == watchlist
    assert strategies.captured_extra == []


async def test_screen_failure_degrades_to_watchlist(tmp_path) -> None:
    watchlist = ["ZZZA", "ZZZB"]
    agent, strategies = await _run_cycle(tmp_path, enabled=True, watchlist=watchlist,
                                         fail_screen=True)

    # The screen raised, so candidates degrade to [] and the cycle proceeds on the
    # watchlist alone — completing (not crashing) exactly once.
    assert agent.calls == 1
    assert agent.captured_watchlist == watchlist
    assert strategies.captured_extra == []
