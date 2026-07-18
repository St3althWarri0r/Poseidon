"""MarketScreener (screener TASK 4): blended-momentum ranking behind a median
20-day dollar-volume floor, a TTL cache, and degrade-to-last-good/``[]``.

No network — a canned fake router serves ``bars_multi`` and the clock is
injected, so the cache TTL is exercised deterministically. The screener only
picks WHICH symbols the AI evaluates; it never trades and never raises — a screen
failure yields the last good cache (or ``[]``) so the cycle degrades to the
watchlist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.core.config import ScreenerConfig
from poseidon.core.errors import ProviderError
from poseidon.core.models import Bar
from poseidon.strategy.screener import MarketScreener, ScoredCandidate


def make_bars(symbol: str, closes: list[float], *, volume: int = 1_000_000) -> list[Bar]:
    """Daily bars with the given chronological (oldest-first) close series."""
    now = datetime.now(UTC)
    n = len(closes)
    bars: list[Bar] = []
    for i, c in enumerate(closes):
        day = now - timedelta(days=n - i)
        px = Decimal(str(c))
        bars.append(
            Bar(symbol=symbol, open=px, high=px, low=px, close=px, volume=volume,
                start=day, end=day, source="test")
        )
    return bars


def series(*, r_1m: float, r_3m: float, final: float = 100.0, length: int = 64) -> list[float]:
    """A close series of ``length`` bars pinned to exact 21d and 63d returns.

    ``pct_return(closes, 21) == r_1m`` (anchor at index -22) and
    ``pct_return(closes, 63) == r_3m`` (anchor at index -64); every other close
    is ``final`` so the last-20 dollar-volume median is a clean ``final*volume``.
    """
    closes = [final] * length
    closes[-1] = final
    closes[-22] = final / (1.0 + r_1m)
    closes[-64] = final / (1.0 + r_3m)
    return closes


class Clock:
    """Injectable monotonic clock; advance with ``+=``-style ``tick``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


class FakeRouter:
    """Canned ``bars_multi`` — returns bars only for known symbols; counts calls
    so cache/refresh behaviour is observable. ``fail`` makes it raise."""

    def __init__(self, bars_by_symbol: dict[str, list[Bar]], *, fail: bool = False) -> None:
        self._bars = bars_by_symbol
        self.fail = fail
        self.calls = 0

    async def bars_multi(self, symbols: list[str], *, timeframe: str = "1d",
                         limit: int = 90) -> dict[str, list[Bar]]:
        self.calls += 1
        if self.fail:
            raise ProviderError("fake", "simulated screen failure")
        return {s: self._bars[s] for s in symbols if s in self._bars}


def make_screener(bars_by_symbol: dict[str, list[Bar]], *, clock: Clock | None = None,
                  fail: bool = False, **cfg_kwargs: object) -> tuple[MarketScreener, FakeRouter, Clock]:
    clock = clock or Clock()
    router = FakeRouter(bars_by_symbol, fail=fail)
    cfg = ScreenerConfig(enabled=True, **cfg_kwargs)  # type: ignore[arg-type]
    screener = MarketScreener(cfg, router, now=clock)  # type: ignore[arg-type]
    return screener, router, clock


async def test_ranks_by_blended_momentum_top_n() -> None:
    bars = {
        "AAPL": make_bars("AAPL", series(r_1m=0.10, r_3m=0.10)),   # score 0.10
        "MSFT": make_bars("MSFT", series(r_1m=0.20, r_3m=0.05)),   # score 0.14
        "NVDA": make_bars("NVDA", series(r_1m=0.30, r_3m=0.30)),   # score 0.30
        "AMZN": make_bars("AMZN", series(r_1m=0.05, r_3m=0.05)),   # score 0.05
        "GOOGL": make_bars("GOOGL", series(r_1m=0.02, r_3m=0.02)),  # score 0.02
    }
    screener, _router, _clock = make_screener(bars, top_n=3)

    result = await screener.select_candidates()

    # sorted by 0.6*r_1m + 0.4*r_3m descending, truncated to top_n
    assert result == ["NVDA", "MSFT", "AAPL"]


async def test_liquidity_floor_excludes_thin_names() -> None:
    # Both names have identical strong momentum; only the liquid one survives the
    # $20M median-20d-dollar-volume floor. Thin: 100 * 100 = $10k median ADV$.
    bars = {
        "AAPL": make_bars("AAPL", series(r_1m=0.30, r_3m=0.30), volume=1_000_000),
        "MSFT": make_bars("MSFT", series(r_1m=0.30, r_3m=0.30), volume=100),
    }
    screener, _router, _clock = make_screener(bars, top_n=10)

    result = await screener.select_candidates()

    assert result == ["AAPL"]


async def test_skips_short_history() -> None:
    # Fewer than 64 bars → cannot compute a 63d return → skipped, not ranked.
    bars = {
        "AAPL": make_bars("AAPL", series(r_1m=0.30, r_3m=0.30)),          # 64 bars
        "MSFT": make_bars("MSFT", [100.0 + i for i in range(40)]),        # only 40 bars
    }
    screener, _router, _clock = make_screener(bars, top_n=10)

    result = await screener.select_candidates()

    assert result == ["AAPL"]
    assert "MSFT" not in result


async def test_caches_within_ttl() -> None:
    bars = {"AAPL": make_bars("AAPL", series(r_1m=0.10, r_3m=0.10))}
    screener, router, clock = make_screener(bars, refresh_minutes=15)

    first = await screener.select_candidates()
    clock.tick(14 * 60)  # still inside the 15-min TTL
    second = await screener.select_candidates()

    assert first == second == ["AAPL"]
    assert router.calls == 1  # re-used the cache, no re-screen


async def test_refreshes_after_ttl() -> None:
    bars = {"AAPL": make_bars("AAPL", series(r_1m=0.10, r_3m=0.10))}
    screener, router, clock = make_screener(bars, refresh_minutes=15)

    await screener.select_candidates()
    clock.tick(15 * 60 + 1)  # past the TTL
    await screener.select_candidates()

    assert router.calls == 2  # cache lapsed → re-screened


async def test_failure_returns_last_cache() -> None:
    bars = {"AAPL": make_bars("AAPL", series(r_1m=0.10, r_3m=0.10))}
    screener, router, clock = make_screener(bars, refresh_minutes=15)

    good = await screener.select_candidates()
    assert good == ["AAPL"]

    # feed goes down and the cache lapses → screen raises → last good cache
    router.fail = True
    clock.tick(15 * 60 + 1)
    result = await screener.select_candidates()

    assert result == ["AAPL"]  # degraded to last good, never raised


async def test_failure_with_empty_cache_returns_empty() -> None:
    bars = {"AAPL": make_bars("AAPL", series(r_1m=0.10, r_3m=0.10))}
    screener, _router, _clock = make_screener(bars, fail=True)

    result = await screener.select_candidates()

    assert result == []  # never raised; empty cache degrades to []


async def test_disabled_returns_empty() -> None:
    bars = {"AAPL": make_bars("AAPL", series(r_1m=0.10, r_3m=0.10))}
    router = FakeRouter(bars)
    cfg = ScreenerConfig(enabled=False)
    screener = MarketScreener(cfg, router)  # type: ignore[arg-type]

    result = await screener.select_candidates()

    assert result == []
    assert router.calls == 0  # disabled → not even a screen attempt


def test_scored_candidate_is_frozen() -> None:
    c = ScoredCandidate(symbol="AAPL", score=0.1, dollar_volume=1.0, r_1m=0.1, r_3m=0.1)
    try:
        c.score = 0.2  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        assert isinstance(exc, (AttributeError, TypeError))
    else:
        raise AssertionError("ScoredCandidate must be frozen")
