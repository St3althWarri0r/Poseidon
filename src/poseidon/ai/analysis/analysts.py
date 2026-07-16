"""The four analysts. Each is ONE tool-less completion producing a structured
AnalystReport; malformed output degrades to a neutral report (never crashes the
fan-out). Advisory only — no tools, no dispatcher, no order path."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import structlog

from ...core.models import AnalystReport
from ..backends import add_usage
from ..backends.base import ChatBackend
from .parse import first_json_obj
from .snapshot import Snapshot

log = structlog.get_logger(__name__)

_JSON_RULES = ('Reply with ONLY a JSON object: {"stance": "bullish|bearish|neutral", '
               '"confidence": 0..1, "summary": "<=2 sentences", "key_points": [..], '
               '"data_gaps": [..], "sources": [..]}. Cite the pinned snapshot numbers; '
               'never invent a price.')

_ROLES: dict[str, str] = {
    "fundamentals": "You are the FUNDAMENTALS analyst. Judge valuation and business quality.",
    "technical": "You are the TECHNICAL analyst. Judge trend, momentum, and levels.",
    "news": "You are the NEWS analyst. Judge catalysts and headline risk from the given text.",
    "sentiment": "You are the MARKET-SENTIMENT analyst. Judge tone/positioning from news "
                 "tone and the snapshot's price/volume momentum (no external social feed).",
}


def _coerce(role: str, obj: dict[str, Any]) -> AnalystReport:
    stance = obj.get("stance")
    if stance not in {"bullish", "bearish", "neutral"}:
        stance = "neutral"
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))

    def _strs(v: object) -> list[str]:
        return [str(x) for x in v] if isinstance(v, list) else []
    return AnalystReport(
        role=role, summary=str(obj.get("summary", ""))[:800], stance=stance,
        confidence=conf, key_points=_strs(obj.get("key_points")),
        data_gaps=_strs(obj.get("data_gaps")), sources=_strs(obj.get("sources")))


async def _one(backend: ChatBackend, role: str, system: str, user: str,
               usage: list[dict[str, int]] | None = None) -> AnalystReport:
    try:
        resp = await backend.complete([{"role": "user", "content": user}],
                                      tools=[], system=system + "\n" + _JSON_RULES)
        add_usage(usage, getattr(resp, "usage", None))
        return _coerce(role, first_json_obj(resp.text or ""))
    except Exception as exc:  # degrade, never crash the fan-out
        add_usage(usage, getattr(exc, "usage", None))
        log.warning("analyst failed", role=role, error=str(exc))
        return AnalystReport(role=role, summary="", stance="neutral", confidence=0.0,
                             key_points=[], data_gaps=[f"{role} analyst unavailable"],
                             sources=[])


async def run_analysts(backend: ChatBackend, snapshot: Snapshot, *, context: str,
                       scan: Callable[[str], str] | None = None,
                       usage: list[dict[str, int]] | None = None) -> list[AnalystReport]:
    safe_ctx = (scan or (lambda s: s))(context)   # sanitize untrusted external text
    user = f"{snapshot.text}\n\nContext:\n{safe_ctx}\n\nProduce your report."
    tasks = [_one(backend, role, system, user, usage) for role, system in _ROLES.items()]
    return list(await asyncio.gather(*tasks))
