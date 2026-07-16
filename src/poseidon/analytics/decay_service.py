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
                 retire: Callable[[str], Awaitable[bool]],
                 account_scope: str = "") -> None:
        """`account_scope` pins health rows to the account whose fills feed the
        sweep (same scoping as the fills themselves), so a paper-era verdict can
        never surface in — or seed hysteresis for — a different account."""
        self._db = db
        self._config = config
        self._load_trips = load_trips
        self._audit = audit_append
        self._notify = notify
        self._retire = retire
        self._account_scope = account_scope

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
        prior = await self._db.get_strategy_health(strategy, account_scope=self._account_scope)
        state = HealthState(prior.state) if prior else HealthState.HEALTHY
        decline = prior.decline_streak if prior else 0
        recover = prior.recover_streak if prior else 0
        a = assess(trips, self._config)
        newest = max(t.exited_at for t in trips)
        seen = await self._db.strategy_health_last_trade_at(
            strategy, account_scope=self._account_scope)
        # Hysteresis counts independent confirmations, not calendar sweeps: with
        # no trade closed since the last assessment the evidence is unchanged,
        # so the state machine holds (the metrics below still refresh).
        frozen = (prior is not None and seen is not None
                  and newest <= seen and a.trades == prior.trades)
        if frozen:
            new_state, new_decline, new_recover = state, decline, recover
        else:
            new_state, new_decline, new_recover = advance(
                state, decline, recover, a.signal, self._config)
        if new_state is not state:
            # Side effects precede the upsert: if one fails, the prior row is
            # untouched and the next sweep recomputes this same transition and
            # retries — a committed transition can never notify twice.
            await self._audit("system", "strategy.health_changed",
                              {"strategy": strategy, "from": state.value, "to": new_state.value})
            if new_state in _DOWNGRADES and is_downgrade(state, new_state):
                await self._notify("warning", {"strategy": strategy, "state": new_state.value,
                                               "window_return": round(a.window_return, 4)})
        if self._config.auto_retire and new_state is HealthState.RETIRE_RECOMMENDED:
            # Level-triggered, not edge-triggered: retire() is idempotent (True
            # only when it actually deactivates a custom strategy), so a
            # transient failure at the transition is re-attempted every sweep
            # instead of being lost with the one-shot transition.
            did = await self._retire(strategy)
            if did:
                await self._audit("system", "strategy.auto_retired", {"strategy": strategy})
        await self._db.upsert_strategy_health(StrategyHealth(
            strategy=strategy, state=new_state.value, decline_streak=new_decline,
            recover_streak=new_recover, window_return=a.window_return,
            baseline_return=a.baseline_return, t_stat=a.t0, trades=a.trades,
            updated_at=datetime.now(UTC)),
            account_scope=self._account_scope, last_trade_at=newest)

    async def report(self) -> list[StrategyHealth]:
        try:
            return await self._db.list_strategy_health(account_scope=self._account_scope)
        except Exception as exc:
            log.warning("strategy-health report failed", error=str(exc))
            return []
