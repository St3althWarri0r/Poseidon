"""Strategy engine: run all enabled strategies and collect their signals."""

from __future__ import annotations

import asyncio

import structlog

from ..core.config import StrategyConfig
from ..core.errors import ConfigError
from ..data.router import DataRouter
from ..portfolio.state import PortfolioState
from .base import Signal, Strategy
from .builtin import BUILTIN_STRATEGIES

log = structlog.get_logger(__name__)

_SCAN_TIMEOUT = 120.0


class StrategyEngine:
    def __init__(self, configs: list[StrategyConfig], default_symbols: list[str]) -> None:
        self._strategies: list[Strategy] = []
        for cfg in configs:
            if not cfg.enabled:
                continue
            cls = BUILTIN_STRATEGIES.get(cfg.name)
            if cls is None:
                raise ConfigError(
                    f"unknown strategy '{cfg.name}'. Available: {', '.join(sorted(BUILTIN_STRATEGIES))}"
                )
            symbols = cfg.symbols or default_symbols
            self._strategies.append(cls(symbols=symbols, options=cfg.options))

    @property
    def enabled_names(self) -> list[str]:
        return [s.name for s in self._strategies]

    async def scan_all(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        """Run every enabled strategy concurrently. One strategy failing (or
        hanging) never blocks the others."""
        if not self._strategies:
            return []

        async def run(strategy: Strategy) -> list[Signal]:
            try:
                return await asyncio.wait_for(strategy.scan(router, portfolio), _SCAN_TIMEOUT)
            except TimeoutError:
                log.warning("strategy scan timed out", strategy=strategy.name)
                return []
            except Exception:
                log.exception("strategy scan failed", strategy=strategy.name)
                return []

        results = await asyncio.gather(*(run(s) for s in self._strategies))
        signals = [signal for batch in results for signal in batch]
        log.info("strategy scan complete", strategies=len(self._strategies), signals=len(signals))
        return signals
