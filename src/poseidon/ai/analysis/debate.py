"""Bull vs bear debate over the structured analyst reports, then a facilitator
verdict. NL is used only inside the debate turns (the 'structured-state, not
telephone' discipline); the output is structured. Advisory only."""
from __future__ import annotations

import structlog

from ...core.models import AnalystReport, DebateVerdict
from ..backends.base import ChatBackend
from .parse import first_json_obj

log = structlog.get_logger(__name__)


def _digest(reports: list[AnalystReport]) -> str:
    return "\n".join(f"- {r.role} [{r.stance} {r.confidence:.2f}]: {r.summary}"
                     for r in reports)


async def _turn(backend: ChatBackend, system: str, transcript: str) -> str:
    try:
        resp = await backend.complete([{"role": "user", "content": transcript}],
                                      tools=[], system=system)
        return (resp.text or "").strip()[:1000]
    except Exception as exc:
        log.warning("debate turn failed", error=str(exc))
        return ""


async def run_debate(backend: ChatBackend, reports: list[AnalystReport], *,
                     rounds: int) -> DebateVerdict:
    base = _digest(reports)
    bull_sys = "You are the BULL researcher. Argue the long case; rebut the bear."
    bear_sys = "You are the BEAR researcher. Argue the short/avoid case; rebut the bull."
    bull_case = bear_case = ""
    for _ in range(rounds):
        bull_case = await _turn(backend, bull_sys,
                                f"Analyst reports:\n{base}\n\nBear said: {bear_case}\nYour case:")
        bear_case = await _turn(backend, bear_sys,
                                f"Analyst reports:\n{base}\n\nBull said: {bull_case}\nYour case:")
    fac_sys = ('You are the debate FACILITATOR. Weigh the cases and reply with ONLY '
               'JSON: {"direction":"long|short|avoid","conviction":0..1,"synthesis":"<=3 sentences"}.')
    try:
        resp = await backend.complete(
            [{"role": "user", "content": f"Reports:\n{base}\n\nBULL:\n{bull_case}\n\nBEAR:\n{bear_case}"}],
            tools=[], system=fac_sys)
        obj = first_json_obj(resp.text or "")
    except Exception as exc:
        log.warning("facilitator failed", error=str(exc))
        obj = {}
    direction = obj.get("direction")
    if direction not in {"long", "short", "avoid"}:
        direction = "avoid"
    try:
        conv = min(1.0, max(0.0, float(obj.get("conviction", 0.0))))
    except (TypeError, ValueError):
        conv = 0.0
    return DebateVerdict(direction=direction, conviction=conv, bull_case=bull_case[:1500],
                         bear_case=bear_case[:1500], synthesis=str(obj.get("synthesis", ""))[:800],
                         rounds=rounds)
