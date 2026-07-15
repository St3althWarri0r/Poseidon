"""Assemble the firm's stages into an AnalysisPacket (pure — no I/O, no model call)."""
from __future__ import annotations

from ...core.models import AnalysisPacket, AnalystReport, DebateVerdict, RiskLens
from .snapshot import Snapshot


def assemble(*, packet_id: str, symbol: str, snapshot: Snapshot,
             reports: list[AnalystReport], verdict: DebateVerdict, risk_lens: RiskLens,
             model: str) -> AnalysisPacket:
    return AnalysisPacket(
        id=packet_id, symbol=symbol, as_of=snapshot.as_of, model=model,
        reports=reports, verdict=verdict, risk_lens=risk_lens,
        snapshot_digest=snapshot.text)
