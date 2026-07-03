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
