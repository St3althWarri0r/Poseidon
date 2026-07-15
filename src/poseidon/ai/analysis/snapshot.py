"""Deterministic numeric snapshot the analysts must cite verbatim.

Anti-confabulation (analysis §3.3): a weak model recalling/inventing prices is a
safety risk. Pinning exact live numbers into text the analysts quote structurally
reduces hallucinated inputs. Live-data-only: every number carries as_of + source.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    as_of: datetime
    source: str
    text: str


async def build_snapshot(router: object, symbol: str) -> Snapshot | None:
    try:
        q = await router.quote(symbol, allow_delayed=True)  # type: ignore[attr-defined]
        bars = await router.bars(symbol, timeframe="1d", limit=30)  # type: ignore[attr-defined]
    except Exception as exc:  # best-effort — a missing snapshot skips the symbol
        log.warning("snapshot failed", symbol=symbol, error=str(exc))
        return None
    closes: list[Any] = []
    for b in bars:
        c = getattr(b, "close", None)
        if c:
            closes.append(c)
    hi = max(closes) if closes else None
    lo = min(closes) if closes else None
    text = (f"{symbol} pinned live snapshot (cite these exact numbers; do not "
            f"invent others): last {q.price}; 30d range {lo}-{hi}; "
            f"as_of {q.as_of.isoformat()}; source {q.source}.")
    return Snapshot(symbol=symbol, as_of=q.as_of, source=q.source, text=text)
