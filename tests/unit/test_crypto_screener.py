"""Crypto screener (whole-market TASK 5): the SAME :class:`MarketScreener`
reused for a crypto ``BASE/USD`` universe via ctor kwargs, not a duplicate class.

A ``CryptoScreenerConfig`` (base-typed) drives blended-momentum ranking behind the
median 20-day dollar-volume floor exactly like the equity screener; the only crypto
differences are absorbed by ``require=DataCapability.CRYPTO`` and ``concurrency=`` —
both threaded straight through to ``router.bars_multi``. The TTL cache and
degrade-to-last-good/``[]`` behaviour are inherited unchanged.

No network — a canned fake router serves ``bars_multi``, records the ``require`` /
``concurrency`` it was handed, and the clock is injected so the TTL is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poseidon.core.config import CryptoScreenerConfig
from poseidon.core.errors import ProviderError
from poseidon.core.models import Bar
from poseidon.data.base import DataCapability
from poseidon.strategy.screener import MarketScreener


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
    """A close series pinned to exact 21d and 63d returns (see equity test)."""
    closes = [final] * length
    closes[-1] = final
    closes[-22] = final / (1.0 + r_1m)
    closes[-64] = final / (1.0 + r_3m)
    return closes


class Clock:
    """Injectable monotonic clock; advance with ``tick``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


class FakeRouter:
    """Canned ``bars_multi`` that ALSO records the ``require`` / ``concurrency`` it
    was passed, so the crypto screener's routing gate is observable in tests."""

    def __init__(self, bars_by_symbol: dict[str, list[Bar]], *, fail: bool = False) -> None:
        self._bars = bars_by_symbol
        self.fail = fail
        self.calls = 0
        self.last_require: DataCapability | None = None
        self.last_concurrency: int | None = None
        self.last_timeframe: str | None = None
        self.last_limit: int | None = None

    async def bars_multi(self, symbols: list[str], *, timeframe: str = "1d",
                         limit: int = 90, require: DataCapability | None = None,
                         concurrency: int | None = None) -> dict[str, list[Bar]]:
        self.calls += 1
        self.last_require = require
        self.last_concurrency = concurrency
        self.last_timeframe = timeframe
        self.last_limit = limit
        if self.fail:
            raise ProviderError("fake", "simulated crypto screen failure")
        return {s: self._bars[s] for s in symbols if s in self._bars}


def make_crypto_screener(bars_by_symbol: dict[str, list[Bar]], *, clock: Clock | None = None,
                         fail: bool = False,
                         **cfg_kwargs: object) -> tuple[MarketScreener, FakeRouter, Clock]:
    clock = clock or Clock()
    router = FakeRouter(bars_by_symbol, fail=fail)
    cfg = CryptoScreenerConfig(enabled=True, **cfg_kwargs)  # type: ignore[arg-type]
    screener = MarketScreener(
        cfg, router,  # type: ignore[arg-type]
        require=DataCapability.CRYPTO, concurrency=cfg.concurrency, now=clock,
    )
    return screener, router, clock


async def test_ranks_base_usd_by_blended_momentum_top_n() -> None:
    # BASE/USD symbols ranked identically to equities: 0.6*r_1m + 0.4*r_3m desc.
    bars = {
        "BTC/USD": make_bars("BTC/USD", series(r_1m=0.10, r_3m=0.10)),   # 0.10
        "ETH/USD": make_bars("ETH/USD", series(r_1m=0.20, r_3m=0.05)),   # 0.14
        "SOL/USD": make_bars("SOL/USD", series(r_1m=0.30, r_3m=0.30)),   # 0.30
        "XRP/USD": make_bars("XRP/USD", series(r_1m=0.05, r_3m=0.05)),   # 0.05
    }
    screener, _router, _clock = make_crypto_screener(bars, top_n=3)

    result = await screener.select_candidates()

    assert result == ["SOL/USD", "ETH/USD", "BTC/USD"]


async def test_threads_require_crypto_and_concurrency_to_bars_multi() -> None:
    bars = {"BTC/USD": make_bars("BTC/USD", series(r_1m=0.10, r_3m=0.10))}
    screener, router, _clock = make_crypto_screener(bars, concurrency=6, bars_limit=90)

    await screener.select_candidates()

    assert router.last_require is DataCapability.CRYPTO  # gated to crypto providers
    assert router.last_concurrency == 6                  # bounded fan-out passed through
    assert router.last_timeframe == "1d"
    assert router.last_limit == 90


async def test_liquidity_floor_reused_for_crypto() -> None:
    # $10M crypto floor: thin name (100 * 100 = $10k median ADV$) excluded.
    bars = {
        "BTC/USD": make_bars("BTC/USD", series(r_1m=0.30, r_3m=0.30), volume=1_000_000),
        "ETH/USD": make_bars("ETH/USD", series(r_1m=0.30, r_3m=0.30), volume=100),
    }
    screener, _router, _clock = make_crypto_screener(bars, top_n=10)

    result = await screener.select_candidates()

    assert result == ["BTC/USD"]


async def test_ttl_cache_reused() -> None:
    bars = {"BTC/USD": make_bars("BTC/USD", series(r_1m=0.10, r_3m=0.10))}
    screener, router, clock = make_crypto_screener(bars, refresh_minutes=15)

    first = await screener.select_candidates()
    clock.tick(14 * 60)  # still inside the TTL
    second = await screener.select_candidates()

    assert first == second == ["BTC/USD"]
    assert router.calls == 1  # cache reused, no re-screen


async def test_failure_degrades_to_empty() -> None:
    bars = {"BTC/USD": make_bars("BTC/USD", series(r_1m=0.10, r_3m=0.10))}
    screener, _router, _clock = make_crypto_screener(bars, fail=True)

    result = await screener.select_candidates()

    assert result == []  # never raised; empty cache degrades to []


async def test_disabled_returns_empty_no_screen() -> None:
    bars = {"BTC/USD": make_bars("BTC/USD", series(r_1m=0.10, r_3m=0.10))}
    router = FakeRouter(bars)
    cfg = CryptoScreenerConfig(enabled=False)
    screener = MarketScreener(
        cfg, router,  # type: ignore[arg-type]
        require=DataCapability.CRYPTO, concurrency=cfg.concurrency,
    )

    result = await screener.select_candidates()

    assert result == []
    assert router.calls == 0  # disabled → not even a screen attempt
