# tests/unit/test_strategy_health_storage.py
from __future__ import annotations

from datetime import UTC, datetime

from poseidon.core.models import StrategyHealth
from poseidon.storage.db import Database


def _h(strategy: str, state: str) -> StrategyHealth:
    return StrategyHealth(strategy=strategy, state=state, decline_streak=1, recover_streak=0,
                          window_return=-0.01, baseline_return=0.02, t_stat=-3.1, trades=10,
                          updated_at=datetime.now(UTC))


async def test_upsert_get_list(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    await db.upsert_strategy_health(_h("alpha", "decaying"))
    await db.upsert_strategy_health(_h("alpha", "retire_recommended"))   # upsert same PK
    got = await db.get_strategy_health("alpha")
    assert got is not None and got.state == "retire_recommended"          # latest wins
    assert len(await db.list_strategy_health()) == 1
    await db.close()
