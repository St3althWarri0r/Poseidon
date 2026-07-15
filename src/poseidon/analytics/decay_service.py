"""Strategy-decay watchdog service. Best-effort, scheduled; reduce-only (its only
mutation is deactivating a decayed custom strategy). Never imports the risk engine,
the order manager, or the execution layer."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from ..core.config import StrategyHealthConfig
from ..core.models import StrategyHealth
from ..storage.db import Database
from .decay import HealthState, advance, assess, is_downgrade
from .performance import RoundTrip

log = structlog.get_logger(__name__)

_DOWNGRADES = {HealthState.DECAYING, HealthState.RETIRE_RECOMMENDED}


class StrategyHealthService:
    def __init__(self, *, db: Database, config: StrategyHealthConfig,
                 load_trips: Callable[[], Awaitable[list[RoundTrip]]],
                 audit_append: Callable[[str, str, dict[str, Any]], Awaitable[Any]],
                 notify: Callable[[str, dict[str, Any]], Awaitable[Any]],
                 retire: Callable[[str], Awaitable[bool]]) -> None:
        self._db = db
        self._config = config
        self._load_trips = load_trips
        self._audit = audit_append
        self._notify = notify
        self._retire = retire

    async def sweep(self, _topic: str | None = None, _payload: object = None) -> None:
        if not self._config.enabled:
            return
        try:
            trips = await self._load_trips()
        except Exception as exc:
            log.warning("strategy-health load failed", error=str(exc))
            return
        by_strategy: dict[str, list[RoundTrip]] = {}
        for t in trips:
            by_strategy.setdefault(t.strategy or "unattributed", []).append(t)
        for strategy, strat_trips in by_strategy.items():
            try:
                await self._evaluate(strategy, strat_trips)
            except Exception as exc:            # one bad strategy can't break the sweep
                log.warning("strategy-health eval failed", strategy=strategy, error=str(exc))

    async def _evaluate(self, strategy: str, trips: list[RoundTrip]) -> None:
        prior = await self._db.get_strategy_health(strategy)
        state = HealthState(prior.state) if prior else HealthState.HEALTHY
        decline = prior.decline_streak if prior else 0
        recover = prior.recover_streak if prior else 0
        a = assess(trips, self._config)
        new_state, decline, recover = advance(state, decline, recover, a.signal, self._config)
        await self._db.upsert_strategy_health(StrategyHealth(
            strategy=strategy, state=new_state.value, decline_streak=decline,
            recover_streak=recover, window_return=a.window_return,
            baseline_return=a.baseline_return, t_stat=a.t0, trades=a.trades,
            updated_at=datetime.now(UTC)))
        if new_state is state:
            return
        await self._audit("system", "strategy.health_changed",
                          {"strategy": strategy, "from": state.value, "to": new_state.value})
        if new_state in _DOWNGRADES and is_downgrade(state, new_state):
            await self._notify("warning", {"strategy": strategy, "state": new_state.value,
                                           "window_return": round(a.window_return, 4)})
        if (self._config.auto_retire and new_state is HealthState.RETIRE_RECOMMENDED):
            did = await self._retire(strategy)          # only deactivates a custom strategy
            if did:
                await self._audit("system", "strategy.auto_retired", {"strategy": strategy})

    async def report(self) -> list[StrategyHealth]:
        try:
            return await self._db.list_strategy_health()
        except Exception as exc:
            log.warning("strategy-health report failed", error=str(exc))
            return []
