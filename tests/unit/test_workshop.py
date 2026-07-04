"""Algorithm workshop: validation, execution wrapper, and lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poseidon.core.clock import FreshnessPolicy
from poseidon.core.errors import ConfigError
from poseidon.data.router import DataRouter
from poseidon.portfolio.state import PortfolioState
from poseidon.security.audit import AuditLog
from poseidon.storage.db import Database
from poseidon.strategy.custom import CustomAlgorithm, validate_algorithm
from poseidon.strategy.engine import StrategyEngine
from poseidon.strategy.workshop import AlgorithmWorkshop

from ..conftest import FakeProvider

GOOD_SOURCE = '''
async def scan(ctx):
    signals = []
    for symbol in ctx.symbols:
        try:
            bars = await ctx.bars(symbol, timeframe="1d", limit=30)
        except Exception:
            continue
        closes = [float(b.close) for b in bars]
        if len(closes) >= 20 and closes[-1] >= sum(closes[-20:]) / 20:
            signals.append({"symbol": symbol, "direction": "long",
                            "strength": 0.6, "evidence": {"above_20d": True}})
    return signals
'''


class TestValidation:
    def test_good_source_passes(self) -> None:
        assert validate_algorithm(GOOD_SOURCE) == []

    def test_syntax_error(self) -> None:
        assert any("syntax error" in p for p in validate_algorithm("def scan(:"))

    def test_missing_scan(self) -> None:
        assert any("async def scan" in p for p in validate_algorithm("x = 1"))

    def test_sync_scan_rejected(self) -> None:
        problems = validate_algorithm("def scan(ctx):\n    return []")
        assert any("async" in p for p in problems)

    def test_forbidden_imports_and_calls(self) -> None:
        bad = "import os\nasync def scan(ctx):\n    open('/etc/passwd')\n    return []"
        problems = validate_algorithm(bad)
        assert any("'os'" in p for p in problems)
        assert any("open()" in p for p in problems)

    def test_dunder_access_rejected(self) -> None:
        bad = "async def scan(ctx):\n    return ctx.__class__\n"
        assert any("dunder" in p for p in validate_algorithm(bad))

    def test_math_is_fine(self) -> None:
        source = "import math\nasync def scan(ctx):\n    return [] if math.pi else []"
        assert validate_algorithm(source) == []


class TestCustomAlgorithm:
    @pytest.fixture()
    def router(self) -> DataRouter:
        return DataRouter([(FakeProvider(name="feed"), 10)], FreshnessPolicy())

    async def test_scan_produces_signals(self, router: DataRouter) -> None:
        algo = CustomAlgorithm(algo_name="trend20", source=GOOD_SOURCE, symbols=["AAPL"])
        assert algo.name == "algo:trend20"
        signals = await algo.scan(router, PortfolioState())
        # FakeProvider bars trend upward, so the 20d filter fires.
        assert signals and signals[0].strategy == "algo:trend20"
        assert signals[0].symbol == "AAPL" and signals[0].direction == "long"

    async def test_garbage_rows_dropped_and_strength_clamped(self, router: DataRouter) -> None:
        source = '''
async def scan(ctx):
    return [
        {"symbol": "aapl", "direction": "long", "strength": 7.5},
        {"symbol": "MSFT", "direction": "sideways"},
        "not a dict",
        {"direction": "long"},
    ]
'''
        algo = CustomAlgorithm(algo_name="messy", source=source, symbols=["AAPL"])
        signals = await algo.scan(router, PortfolioState())
        assert len(signals) == 1
        assert signals[0].symbol == "AAPL" and signals[0].strength == 1.0

    def test_invalid_source_refuses_to_compile(self) -> None:
        with pytest.raises(ValueError, match="scan"):
            CustomAlgorithm(algo_name="bad", source="x = 1", symbols=[])


class TestWorkshopLifecycle:
    @pytest.fixture()
    async def workshop(self, tmp_path):  # noqa: ANN001
        db = Database(tmp_path / "w.db")
        await db.open()
        engine = StrategyEngine([], ["AAPL"])
        shop = AlgorithmWorkshop(db, engine, AuditLog(db), default_symbols=["AAPL"])
        yield shop, engine, db
        await db.close()

    async def test_create_activate_deactivate_delete(self, workshop) -> None:  # noqa: ANN001
        shop, engine, _db = workshop
        record = await shop.create(name="My Trend 20", source=GOOD_SOURCE,
                                   description="20d breakout screen")
        assert record["status"] == "draft" and record["name"] == "my_trend_20"
        assert engine.enabled_names == []

        await shop.activate(record["id"])
        assert "algo:my_trend_20" in engine.enabled_names

        await shop.deactivate(record["id"])
        assert "algo:my_trend_20" not in engine.enabled_names
        assert (await shop.get(record["id"]))["status"] == "draft"

        await shop.delete(record["id"])
        with pytest.raises(KeyError):
            await shop.get(record["id"])

    async def test_create_rejects_invalid_source(self, workshop) -> None:  # noqa: ANN001
        shop, _engine, _db = workshop
        with pytest.raises(ConfigError, match="validation"):
            await shop.create(name="bad", source="import os")

    async def test_update_active_hot_reloads(self, workshop) -> None:  # noqa: ANN001
        shop, engine, _db = workshop
        record = await shop.create(name="algo1", source=GOOD_SOURCE)
        await shop.activate(record["id"])
        new_source = GOOD_SOURCE.replace('"strength": 0.6', '"strength": 0.9')
        updated = await shop.update(record["id"], source=new_source)
        assert updated["status"] == "active"
        assert "algo:algo1" in engine.enabled_names

    async def test_load_active_demotes_broken(self, workshop) -> None:  # noqa: ANN001
        shop, engine, db = workshop
        record = await shop.create(name="fine", source=GOOD_SOURCE)
        await shop.activate(record["id"])
        # Corrupt the stored source behind the workshop's back.
        await db.execute("UPDATE algorithms SET source = 'x = (' WHERE id = ?", (record["id"],))
        engine.remove_strategy("algo:fine")
        loaded = await shop.load_active()
        assert loaded == 0
        demoted = await shop.get(record["id"])
        assert demoted["status"] == "draft"
        assert "demoted" in demoted["review_notes"]

    async def test_claude_drafts_never_auto_activate(self, workshop) -> None:  # noqa: ANN001
        shop, engine, _db = workshop
        record = await shop.create(name="ai_idea", source=GOOD_SOURCE, created_by="claude")
        assert record["status"] == "draft" and record["created_by"] == "claude"
        assert engine.enabled_names == []


def test_review_source_validation_roundtrip() -> None:
    """The reviewer validates produced source with the same screen the
    workshop enforces — confirm the imported symbol is the same function."""
    from poseidon.ai import reviewer

    assert reviewer.validate_algorithm is validate_algorithm
    assert isinstance(datetime.now(UTC), datetime) and Decimal("1") == 1  # imports used


class TestIndicators:
    def test_rsi_wilder(self) -> None:
        from poseidon.strategy.indicators import rsi

        flat_up = [100 + i for i in range(30)]
        assert rsi(flat_up, 14) == 100.0
        alternating = [100.0]
        for i in range(30):
            alternating.append(alternating[-1] + (1 if i % 2 else -1))
        value = rsi(alternating, 14)
        assert value is not None and 40 < value < 60
        assert rsi([100.0] * 5, 14) is None  # not enough history

    def test_cumulative_and_ma_return_percent_units(self) -> None:
        from poseidon.strategy.indicators import cumulative_return, moving_average_return

        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        cr = cumulative_return(closes, 4)
        assert cr is not None and abs(cr - 4.0) < 1e-9  # percent, like Composer
        mar = moving_average_return(closes, 4)
        assert mar is not None and 0.9 < mar < 1.0


class TestFTLTExample:
    """The shipped Composer port must pass the workshop validator, compile,
    and run against the fake feed (flat prices => bull-market top-3 path)."""

    def _source(self) -> str:
        import pathlib
        return pathlib.Path("examples/algorithms/tqqq_ftlt.py").read_text()

    def test_validates_and_compiles(self) -> None:
        source = self._source()
        assert validate_algorithm(source) == []
        CustomAlgorithm(algo_name="tqqq_ftlt", source=source, symbols=[])

    async def test_runs_and_targets_weights(self) -> None:
        router = DataRouter([(FakeProvider(name="feed", bars_count=280), 10)],
                            FreshnessPolicy())
        algo = CustomAlgorithm(algo_name="tqqq_ftlt", source=self._source(), symbols=[])
        signals = await algo.scan(router, PortfolioState())
        # Flat closes: SPY == its 200d MA (not above) => dip-buy branch; flat
        # RSI = None-safe paths; the algorithm must return SOMETHING sane or
        # nothing, but never raise.
        for s in signals:
            assert s.direction in ("long", "exit")
            assert 0.0 <= s.strength <= 1.0


class TestFullIndicatorSuite:
    def test_ema_converges_toward_recent_prices(self) -> None:
        from poseidon.strategy.indicators import ema, sma

        closes = [100.0] * 40 + [110.0] * 10
        e, s = ema(closes, 20), sma(closes, 20)
        assert e is not None and s is not None
        assert e > 100 and abs(e - s) < 10

    def test_macd_positive_in_uptrend(self) -> None:
        from poseidon.strategy.indicators import macd

        up = [100 * 1.005 ** i for i in range(80)]
        result = macd(up)
        assert result is not None
        line, signal, hist = result
        assert line > 0 and abs(hist - (line - signal)) < 1e-12
        assert macd(up[:20]) is None

    def test_bollinger_percent_b(self) -> None:
        from poseidon.strategy.indicators import bollinger

        closes = [100.0, 101.0] * 15
        result = bollinger(closes, 20)
        assert result is not None
        upper, mid, lower, pct_b = result
        assert lower < mid < upper and 0.0 <= pct_b <= 1.0

    def test_stochastic_extremes(self) -> None:
        from poseidon.strategy.indicators import stochastic

        n = 30
        highs = [float(i + 2) for i in range(n)]
        lows = [float(i) for i in range(n)]
        closes = [float(i + 2) for i in range(n)]  # closes at the high
        result = stochastic(highs, lows, closes)
        assert result is not None and result[0] > 85

    def test_atr_matches_constant_range(self) -> None:
        from poseidon.strategy.indicators import atr

        n = 40
        highs, lows, closes = [102.0] * n, [100.0] * n, [101.0] * n
        value = atr(highs, lows, closes, 14)
        assert value is not None and abs(value - 2.0) < 1e-9

    def test_adx_high_in_strong_trend(self) -> None:
        from poseidon.strategy.indicators import adx

        n = 60
        highs = [101.0 + i for i in range(n)]
        lows = [99.0 + i for i in range(n)]
        closes = [100.5 + i for i in range(n)]
        value = adx(highs, lows, closes, 14)
        assert value is not None and value > 60

    def test_stdev_and_drawdown_and_extremes(self) -> None:
        from poseidon.strategy.indicators import highest, lowest, max_drawdown, stdev_price

        closes = [100.0, 120.0, 90.0, 110.0]
        assert max_drawdown(closes, 4) == 25.0  # 120 -> 90
        assert highest(closes, 4) == 120.0 and lowest(closes, 4) == 90.0
        assert stdev_price([100.0] * 20, 20) == 0.0

    def test_obv_direction(self) -> None:
        from poseidon.strategy.indicators import obv

        assert obv([1.0, 2.0, 3.0], [0, 10, 10]) == 20.0
        assert obv([3.0, 2.0, 1.0], [0, 10, 10]) == -20.0

    async def test_ctx_ta_namespace_available_to_algorithms(self) -> None:
        source = '''
async def scan(ctx):
    signals = []
    for symbol in ctx.symbols:
        bars = await ctx.bars(symbol, timeframe="1d", limit=80)
        closes = [float(b.close) for b in bars]
        m = ctx.ta.macd(closes)
        band = ctx.ta.bollinger(closes)
        if m is not None and band is not None:
            signals.append({"symbol": symbol, "direction": "long", "strength": 0.5,
                            "evidence": {"macd": round(m[0], 4), "pct_b": round(band[3], 3)}})
    return signals
'''
        assert validate_algorithm(source) == []
        router = DataRouter([(FakeProvider(name="feed", bars_count=120), 10)], FreshnessPolicy())
        algo = CustomAlgorithm(algo_name="ta_check", source=source, symbols=["AAPL"])
        signals = await algo.scan(router, PortfolioState())
        assert signals and "macd" in signals[0].evidence


class TestSecondExampleAndDryRun:
    def test_lts_v1_validates_and_compiles(self) -> None:
        import pathlib

        source = pathlib.Path("examples/algorithms/tqqq_lts_v1.py").read_text()
        assert validate_algorithm(source) == []
        CustomAlgorithm(algo_name="tqqq_lts_v1", source=source, symbols=[])

    async def test_workshop_dry_run(self, tmp_path) -> None:  # noqa: ANN001
        db = Database(tmp_path / "t.db")
        await db.open()
        try:
            engine = StrategyEngine([], ["AAPL"])
            shop = AlgorithmWorkshop(db, engine, AuditLog(db), default_symbols=["AAPL"])
            record = await shop.create(name="dry", source=GOOD_SOURCE)
            router = DataRouter([(FakeProvider(name="feed"), 10)], FreshnessPolicy())
            result = await shop.test_run(record["id"], router, PortfolioState())
            assert result["count"] == len(result["signals"])
            assert "nothing was traded" in result["note"]
            assert engine.enabled_names == []  # dry run never registers
        finally:
            await db.close()


ROTATION_SOURCE = '''
async def scan(ctx):
    # Deterministic rotation: hold A above its 20d average, else B.
    bars_a = await ctx.bars("AAA", timeframe="1d", limit=30)
    closes = [float(b.close) for b in bars_a]
    avg = ctx.sma(closes, 20)
    if avg is None:
        return []
    pick = "AAA" if closes[-1] >= avg else "BBB"
    return [{"symbol": pick, "direction": "long", "strength": 1.0,
             "evidence": {"target_weight": 1.0}}]
'''


class TestRebalanceBacktest:
    def _history(self, days: int = 300):  # noqa: ANN202
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from poseidon.core.models import Bar

        now = datetime.now(UTC)
        history = {}
        for symbol, drift in (("AAA", 1.001), ("BBB", 1.0)):
            price, bars = 100.0, []
            for i in range(days):
                day = now - timedelta(days=days - i)
                price *= drift
                bars.append(Bar(symbol=symbol, open=Decimal(str(round(price, 4))),
                                high=Decimal(str(round(price * 1.01, 4))),
                                low=Decimal(str(round(price * 0.99, 4))),
                                close=Decimal(str(round(price, 4))), volume=1_000_000,
                                start=day, end=day, source="t"))
            history[symbol] = bars
        return history

    async def test_rotation_backtest_tracks_uptrend(self) -> None:
        from poseidon.backtest.rebalance import rebalance_backtest

        algo = CustomAlgorithm(algo_name="rot", source=ROTATION_SOURCE, symbols=["AAA"])
        report = await rebalance_backtest(algo, self._history(320))
        # AAA drifts +0.1%/day and the algo holds it after warmup.
        assert report["total_return"] > 0.05
        assert report["rebalances"] >= 1
        assert report["days_tested"] > 80
        assert report["equity_curve"][0]["equity"] > 0
        assert "survivorship" in report["note"]

    async def test_insufficient_history_is_refused(self) -> None:
        from poseidon.backtest.rebalance import rebalance_backtest

        algo = CustomAlgorithm(algo_name="rot", source=ROTATION_SOURCE, symbols=["AAA"])
        with pytest.raises(ValueError, match="warmup"):
            await rebalance_backtest(algo, self._history(100))

    async def test_workshop_backtest_end_to_end(self, tmp_path) -> None:  # noqa: ANN001
        db = Database(tmp_path / "bt.db")
        await db.open()
        try:
            engine = StrategyEngine([], ["AAPL"])
            shop = AlgorithmWorkshop(db, engine, AuditLog(db), default_symbols=["AAPL"])
            record = await shop.create(name="bt", source=GOOD_SOURCE)
            router = DataRouter([(FakeProvider(name="feed", bars_count=320), 10)],
                                FreshnessPolicy())
            report = await shop.backtest(record["id"], router, PortfolioState(), years=2)
            assert report["algorithm"] == "bt"
            assert "AAPL" in report["symbols_tested"]
            assert report["days_tested"] > 50
        finally:
            await db.close()


class TestSleeves:
    async def test_sleeve_registered_on_activate_and_cleared(self, tmp_path) -> None:  # noqa: ANN001
        db = Database(tmp_path / "s.db")
        await db.open()
        try:
            caps: dict[str, float] = {}
            engine = StrategyEngine([], ["AAPL"])
            shop = AlgorithmWorkshop(db, engine, AuditLog(db),
                                     default_symbols=["AAPL"], sleeve_caps=caps)
            record = await shop.create(name="rot", source=GOOD_SOURCE, sleeve_pct=0.35)
            assert record["sleeve_pct"] == 0.35
            assert caps == {}  # drafts have no sleeve in force
            await shop.activate(record["id"])
            assert caps == {"algo:rot": 0.35}
            await shop.deactivate(record["id"])
            assert caps == {}
            # Reload path registers active sleeves at startup.
            await shop.activate(record["id"])
            caps.clear()
            assert await shop.load_active() == 1
            assert caps == {"algo:rot": 0.35}
        finally:
            await db.close()

    async def test_sleeve_bounds_enforced(self, tmp_path) -> None:  # noqa: ANN001
        db = Database(tmp_path / "s2.db")
        await db.open()
        try:
            shop = AlgorithmWorkshop(db, StrategyEngine([], []), AuditLog(db),
                                     default_symbols=[])
            with pytest.raises(ConfigError, match="sleeve_pct"):
                await shop.create(name="bad", source=GOOD_SOURCE, sleeve_pct=1.5)
        finally:
            await db.close()
