"""Integration (screener TASK 6): dual-screener wiring in ``run_review_cycle``.

Each cycle the PM's universe is ``watchlist ∪ equity_candidates ∪ crypto_candidates``:

  * the **equity** screener contributes only when the market is open (session is
    not CLOSED) — equities do not trade overnight/weekends;
  * the **crypto** screener contributes **unconditionally** (crypto is 24/7);
  * the fixed **watchlist** is always included (default empty).

The union is order-stable and case-insensitively de-duplicated (watchlist first,
then equity, then crypto), forwarded to the strategies as ``extra_symbols``, and
each candidate's screen rationale is rendered into a compact, always-included
ranked block handed to the agent. Screeners are advisory selection only — every
candidate still flows PM → RiskEngine → broker unchanged.

Safety invariants asserted here:
  * ENABLED equity + crypto ⇒ both sets are appended to the watchlist (union) and
    handed to the strategies as ``extra_symbols``.
  * market CLOSED ⇒ equity candidates are excluded but crypto still contributes.
  * DISABLED / failed screen ⇒ degrades to the watchlist; the cycle never crashes.
  * both screeners ``[]`` + empty watchlist ⇒ a valid portfolio-only cycle that
    completes exactly once (empty ``symbols``, no crash).
  * the ranked candidate block reaches the agent with per-candidate metrics.
"""

from __future__ import annotations

from datetime import UTC, datetime

from poseidon.app import ApplicationKernel
from poseidon.core.clock import FreshnessPolicy
from poseidon.core.config import (
    AppConfig,
    CryptoScreenerConfig,
    ScreenerConfig,
    WatchlistConfig,
)
from poseidon.core.enums import DecisionAction, MarketSession, TradingMode
from poseidon.core.models import Decision
from poseidon.data.base import DataCapability
from poseidon.data.router import DataRouter
from poseidon.security.audit import AuditLog
from poseidon.security.vault import Vault
from poseidon.storage.db import Database
from poseidon.strategy.screener import MarketScreener, ScoredCandidate

from ..conftest import FakeBatchProvider


class _StubAgent:
    """Captures the ``watchlist`` and the ranked ``screener_candidates`` block the
    cycle hands the PM; returns a flat NO_ACTION decision so the cycle persists
    cleanly and never reaches the order path."""

    def __init__(self) -> None:
        self.captured_watchlist: list[str] | None = None
        self.captured_candidates: list[str] | None = None
        self.calls = 0

    async def run_cycle(self, *, mode: object, watchlist: list[str],
                        enabled_strategies: object, strategy_signals: object,
                        market_session: object, market_regime: object = None,
                        trade_lessons: object = None, analysis_packets: object = None,
                        instrument_identities: object = None,
                        screener_candidates: list[str] | None = None) -> Decision:
        self.captured_watchlist = list(watchlist)
        self.captured_candidates = None if screener_candidates is None else list(screener_candidates)
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


class _StubClock:
    """Deterministic session so equity-gating is not wall-clock dependent."""

    def __init__(self, session: MarketSession) -> None:
        self._session = session

    def session(self, at: object = None) -> MarketSession:
        return self._session


class _FakeScreener:
    """In-memory screener with a fixed ranked list — exercises the wiring without
    the router. Mirrors :class:`MarketScreener`'s dual surface (``ranked_candidates``
    for the metrics block, ``select_candidates`` for the bare symbols)."""

    def __init__(self, ranked: list[ScoredCandidate]) -> None:
        self._ranked = list(ranked)

    async def ranked_candidates(self) -> list[ScoredCandidate]:
        return list(self._ranked)

    async def select_candidates(self) -> list[str]:
        return [c.symbol for c in self._ranked]


def _sc(symbol: str, score: float = 0.1) -> ScoredCandidate:
    return ScoredCandidate(symbol=symbol, score=score, dollar_volume=1e9,
                           r_1m=score, r_3m=score * 1.5)


# ------------------------------------------------------------------ real-equity harness


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
    kernel.clock = _StubClock(MarketSession.REGULAR)  # type: ignore[assignment]  # market open
    kernel.screener = MarketScreener(cfg.screener, kernel.router)
    # Crypto screener OFF for the equity-focused cases → contributes nothing.
    kernel.crypto_screener = MarketScreener(
        CryptoScreenerConfig(enabled=False), kernel.router,
        require=DataCapability.CRYPTO)

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


# ------------------------------------------------------------------ fake dual harness


async def _run_dual(tmp_path, *, equity: list[ScoredCandidate],
                    crypto: list[ScoredCandidate], watchlist: list[str],
                    session: MarketSession) -> tuple[_StubAgent, _StubStrategies]:
    cfg = AppConfig(data_dir=tmp_path)
    if watchlist:
        cfg.watchlists = [WatchlistConfig(name="t", symbols=watchlist)]
    cfg.ai.snapshot.identity = False

    kernel = ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))
    kernel.db = Database(tmp_path / "t.db")
    await kernel.db.open()
    kernel.audit = AuditLog(kernel.db)
    kernel.router = None  # type: ignore[assignment]  # fakes never touch the router
    kernel.risk = _StubRisk()  # type: ignore[assignment]
    strategies = _StubStrategies()
    kernel.strategies = strategies  # type: ignore[assignment]
    kernel.order_manager = _StubOrderManager()  # type: ignore[assignment]
    agent = _StubAgent()
    kernel.agent = agent  # type: ignore[assignment]
    kernel.clock = _StubClock(session)  # type: ignore[assignment]
    kernel.screener = _FakeScreener(equity)  # type: ignore[assignment]
    kernel.crypto_screener = _FakeScreener(crypto)  # type: ignore[assignment]

    async def _no_regime() -> None:
        return None
    kernel._regime_line = _no_regime  # type: ignore[method-assign]

    try:
        await kernel.run_review_cycle()
    finally:
        await kernel.db.close()
        await kernel.bus.close()
    return agent, strategies


# ------------------------------------------------------------------ equity-only (real)


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


# ------------------------------------------------------------------ dual screener (fake)


async def test_dual_union_when_market_open(tmp_path) -> None:
    agent, strategies = await _run_dual(
        tmp_path, equity=[_sc("AAA"), _sc("BBB")], crypto=[_sc("BTC/USD"), _sc("ETH/USD")],
        watchlist=["ZZZA"], session=MarketSession.REGULAR)
    # watchlist first, then equity, then crypto — order-stable union.
    assert agent.captured_watchlist == ["ZZZA", "AAA", "BBB", "BTC/USD", "ETH/USD"]
    # Only the screened candidates (not the watchlist) become extra_symbols.
    assert strategies.captured_extra == ["AAA", "BBB", "BTC/USD", "ETH/USD"]


async def test_equity_excluded_when_market_closed_crypto_still_contributes(tmp_path) -> None:
    agent, strategies = await _run_dual(
        tmp_path, equity=[_sc("AAA"), _sc("BBB")], crypto=[_sc("BTC/USD")],
        watchlist=["ZZZA"], session=MarketSession.CLOSED)
    # Market closed → equity candidates are dropped; crypto contributes 24/7.
    assert agent.captured_watchlist == ["ZZZA", "BTC/USD"]
    assert strategies.captured_extra == ["BTC/USD"]


async def test_crypto_contributes_in_every_session(tmp_path) -> None:
    for session in (MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS,
                    MarketSession.REGULAR, MarketSession.CLOSED):
        agent, _ = await _run_dual(
            tmp_path, equity=[], crypto=[_sc("BTC/USD")],
            watchlist=[], session=session)
        assert agent.captured_watchlist == ["BTC/USD"], session


async def test_both_empty_and_empty_watchlist_completes_no_crash(tmp_path) -> None:
    agent, strategies = await _run_dual(
        tmp_path, equity=[], crypto=[], watchlist=[], session=MarketSession.REGULAR)
    # A valid portfolio-only cycle: empty symbols, extra_symbols [], one clean run.
    assert agent.calls == 1
    assert agent.captured_watchlist == []
    assert strategies.captured_extra == []


async def test_ranked_candidate_block_reaches_agent_with_metrics(tmp_path) -> None:
    agent, _ = await _run_dual(
        tmp_path, equity=[_sc("AAA", 0.12)], crypto=[_sc("BTC/USD", 0.20)],
        watchlist=["ZZZA"], session=MarketSession.REGULAR)
    lines = agent.captured_candidates
    assert lines is not None and len(lines) == 2  # one per screened candidate
    joined = "\n".join(lines)
    # Each candidate carries its screen rationale (symbol + momentum metrics).
    assert "AAA" in joined and "BTC/USD" in joined
    assert "score=" in joined and "r1m=" in joined and "r3m=" in joined and "adv$=" in joined


async def test_dedup_across_screeners_is_case_insensitive(tmp_path) -> None:
    # A symbol appearing in both screeners (or matching the watchlist case-folded)
    # is not duplicated in the union.
    agent, strategies = await _run_dual(
        tmp_path, equity=[_sc("AAA")], crypto=[_sc("aaa"), _sc("BTC/USD")],
        watchlist=["ZZZA"], session=MarketSession.REGULAR)
    assert agent.captured_watchlist == ["ZZZA", "AAA", "BTC/USD"]
    assert strategies.captured_extra == ["AAA", "BTC/USD"]
