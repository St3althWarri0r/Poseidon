"""Advisory risk lens: three risk-appetite voices + a synthesis.

NOT the risk engine. This produces COMMENTARY only — it cannot approve, size, or
block a trade. Poseidon's deterministic RiskEngine remains the sole pre-trade gate
(analysis §4.1). Kept structurally separate so the two can never be confused."""
from __future__ import annotations

import structlog

from ...core.models import AnalystReport, DebateVerdict, RiskLens
from ..backends import add_usage
from ..backends.base import ChatBackend

log = structlog.get_logger(__name__)

_VOICES = {
    "aggressive": "You are the RISK-SEEKING voice. Where is upside being underweighted?",
    "neutral": "You are the BALANCED risk voice. State the base-rate risk/reward.",
    "conservative": "You are the RISK-AVERSE voice. What could go wrong; what would you avoid?",
}


async def _voice(backend: ChatBackend, system: str, ctx: str,
                 usage: list[dict[str, int]] | None = None) -> str:
    try:
        resp = await backend.complete([{"role": "user", "content": ctx}],
                                      tools=[], system=system + " Advisory only; you cannot "
                                      "place, size, or block a trade. 2-3 sentences.")
        add_usage(usage, getattr(resp, "usage", None))
        return (resp.text or "").strip()[:800]
    except Exception as exc:
        add_usage(usage, getattr(exc, "usage", None))
        log.warning("risk voice failed", error=str(exc))
        return ""


async def run_risk_lens(backend: ChatBackend, verdict: DebateVerdict,
                        reports: list[AnalystReport], *, rounds: int,
                        usage: list[dict[str, int]] | None = None) -> RiskLens:
    ctx = (f"Firm view: {verdict.direction} (conviction {verdict.conviction:.2f}). "
           f"Synthesis: {verdict.synthesis}")
    out: dict[str, str] = {}
    for _ in range(rounds):                       # later rounds refine over earlier text
        for name, system in _VOICES.items():
            prior = f" Prior note: {out.get(name, '')}" if out.get(name) else ""
            out[name] = await _voice(backend, system, ctx + prior, usage)
    synth = ""
    if any(out.values()):
        synth = await _voice(
            backend, "Synthesize the three risk voices into one advisory paragraph.",
            f"aggressive: {out.get('aggressive','')}\nneutral: {out.get('neutral','')}\n"
            f"conservative: {out.get('conservative','')}", usage)
    return RiskLens(aggressive=out.get("aggressive", ""), neutral=out.get("neutral", ""),
                    conservative=out.get("conservative", ""), synthesis=synth)
