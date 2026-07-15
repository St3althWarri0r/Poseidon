"""Thin async history loader for the research CLI (the only I/O in research/)."""
from __future__ import annotations

from typing import Any

import structlog

from ..core.models import Bar

log = structlog.get_logger(__name__)


async def load_history(router: Any, symbols: list[str], days: int) -> dict[str, list[Bar]]:
    hist: dict[str, list[Bar]] = {}
    for symbol in symbols:
        try:
            bars = await router.bars(symbol, timeframe="1d", limit=days)
        except Exception as exc:                    # a bad symbol must not abort the run
            log.warning("history load failed", symbol=symbol, error=str(exc))
            continue
        if bars:
            hist[symbol] = bars
    return hist
