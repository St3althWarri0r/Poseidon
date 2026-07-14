"""Turn a symbol's fill history into a just-closed position episode, and compute
benchmark-relative return. Advisory inputs to the reflection loop — pure
functions over data Poseidon already recorded (point-in-time safe)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..core.enums import OrderSide
from ..core.models import Bar
from .performance import FillRecord, build_round_trips

_ADD_SIDES = {OrderSide.BUY, OrderSide.BUY_TO_OPEN, OrderSide.BUY_TO_CLOSE}


@dataclass
class ClosedEpisode:
    symbol: str
    strategy: str
    decision_id: str
    is_short: bool
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entered_at: datetime
    exited_at: datetime
    realized_return: float
    holding_days: float


def latest_closed_episode(fills: list[FillRecord]) -> ClosedEpisode | None:
    """The most-recent net-flat episode for a single symbol, or None if the
    symbol is currently open (net != 0) or has no completed episode."""
    ordered = sorted(fills, key=lambda f: f.at)
    net = Decimal(0)
    start: int | None = None
    episodes: list[list[FillRecord]] = []
    for i, f in enumerate(ordered):
        if net == 0 and f.quantity != 0:
            start = i
        net += f.quantity if f.side in _ADD_SIDES else -f.quantity
        if net == 0 and start is not None:
            episodes.append(ordered[start:i + 1])
            start = None
    if net != 0 or not episodes:
        return None
    trips = build_round_trips(episodes[-1])
    if not trips:
        return None
    qty = sum((t.quantity for t in trips), Decimal(0))
    if qty <= 0:
        return None
    entry_notional = sum((t.entry_price * t.quantity for t in trips), Decimal(0))
    exit_notional = sum((t.exit_price * t.quantity for t in trips), Decimal(0))
    pnl = sum((t.pnl for t in trips), Decimal(0))
    entered_at = min(t.entered_at for t in trips)
    exited_at = max(t.exited_at for t in trips)
    return ClosedEpisode(
        symbol=trips[0].symbol, strategy=trips[0].strategy,
        decision_id=trips[0].decision_id, is_short=trips[0].is_short, quantity=qty,
        entry_price=entry_notional / qty, exit_price=exit_notional / qty,
        entered_at=entered_at, exited_at=exited_at,
        realized_return=float(pnl / entry_notional) if entry_notional > 0 else 0.0,
        holding_days=max((exited_at - entered_at).total_seconds() / 86400, 0.0),
    )


def benchmark_return(bars: list[Bar], start: datetime, end: datetime) -> float | None:
    """Close-to-close benchmark return over [start, end]; None if the window is
    not covered or resolves to a single (sub-day) bar."""
    if not bars:
        return None
    ordered = sorted(bars, key=lambda b: b.end)

    def close_asof(dt: datetime) -> Bar | None:
        chosen: Bar | None = None
        for b in ordered:
            if b.end <= dt:
                chosen = b
            else:
                break
        return chosen

    b0, b1 = close_asof(start), close_asof(end)
    if b0 is None or b1 is None or b0.end.date() == b1.end.date():
        return None
    p0, p1 = float(b0.close), float(b1.close)
    return (p1 / p0 - 1) if p0 > 0 else None
