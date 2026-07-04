"""Transaction cost analysis (execution quality).

Implementation-shortfall accounting from the platform's own records: the
arrival price is the live mid captured at the moment the order passed its
final risk validation; slippage is the signed difference to the average
fill, in basis points, where positive always means cost to the account
(paid more on a buy, received less on a sell). Best-execution review is a
first-class report, not a spreadsheet exercise.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.enums import OrderSide, OrderStatus

_TERMINAL_SUBMITTED = {
    OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.CANCELED.value, OrderStatus.EXPIRED.value,
    OrderStatus.REJECTED_BROKER.value,
}


def slippage_bps(side: OrderSide, arrival: Decimal, fill: Decimal) -> float | None:
    """Signed execution cost in basis points (positive = cost)."""
    if arrival <= 0:
        return None
    signed = (fill - arrival) / arrival
    if not side.is_buy:
        signed = -signed
    return float(signed) * 10_000


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        delta = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return None
    return delta if delta >= 0 else None


def execution_quality(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate TCA over persisted order payloads (as stored by the order
    manager). Orders without slippage data (unfilled, or filled before TCA
    existed) are counted but excluded from cost statistics."""
    filled = [o for o in orders if o.get("status") == OrderStatus.FILLED.value]
    reached_broker = [o for o in orders if o.get("status") in _TERMINAL_SUBMITTED]
    measured = [o for o in filled if isinstance(o.get("slippage_bps"), int | float)]
    costs = sorted(float(o["slippage_bps"]) for o in measured)

    by_side: dict[str, list[float]] = defaultdict(list)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for o in measured:
        by_side["buy" if "buy" in str(o.get("side", "")) else "sell"].append(float(o["slippage_bps"]))
        by_symbol[str(o.get("symbol", "?")).upper()].append(float(o["slippage_bps"]))

    fill_seconds = [
        s for o in filled
        if (s := _seconds_between(o.get("created_at"), o.get("updated_at"))) is not None
    ]

    def _avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    worst = max(measured, key=lambda o: float(o["slippage_bps"]), default=None)
    return {
        "orders_filled": len(filled),
        "orders_reaching_broker": len(reached_broker),
        "fill_rate": round(len(filled) / len(reached_broker), 3) if reached_broker else None,
        "orders_measured": len(measured),
        "avg_slippage_bps": _avg(costs),
        "median_slippage_bps": round(statistics.median(costs), 2) if costs else None,
        "worst_slippage_bps": round(costs[-1], 2) if costs else None,
        "worst_fill": {
            "symbol": worst.get("symbol"), "side": worst.get("side"),
            "slippage_bps": round(float(worst["slippage_bps"]), 2),
            "at": worst.get("updated_at"),
        } if worst else None,
        "avg_slippage_bps_by_side": {k: _avg(v) for k, v in sorted(by_side.items())},
        "avg_slippage_bps_by_symbol": {k: _avg(v) for k, v in sorted(by_symbol.items())},
        "avg_seconds_to_fill": round(sum(fill_seconds) / len(fill_seconds), 1) if fill_seconds else None,
    }
