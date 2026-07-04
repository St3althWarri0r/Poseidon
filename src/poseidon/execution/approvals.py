"""Human approval workflow (Mode 2).

Proposed orders wait in a queue surfaced on the dashboard and through
notifications. Each entry carries the full explainability report so the
human decides with the same information the AI had. Approvals expire —
market conditions move on, and a stale approval must never execute.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import structlog

from ..core.events import EventBus, Topics
from ..core.models import Decision, Order

log = structlog.get_logger(__name__)

APPROVAL_TTL_SECONDS = 15 * 60


@dataclass
class PendingApproval:
    order: Order
    decision: Decision
    created_at: float = field(default_factory=time.monotonic)
    resolved: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool | None = None
    resolver: str = ""

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > APPROVAL_TTL_SECONDS

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, APPROVAL_TTL_SECONDS - (time.monotonic() - self.created_at))


class ApprovalQueue:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._pending: dict[str, PendingApproval] = {}

    async def request(self, order: Order, decision: Decision) -> PendingApproval:
        entry = PendingApproval(order=order, decision=decision)
        self._pending[order.id] = entry
        await self._bus.publish(
            Topics.APPROVAL_REQUESTED,
            {
                "order_id": order.id,
                "order": order.model_dump(mode="json"),
                "rationale": decision.rationale.model_dump(mode="json") if decision.rationale else None,
                "expires_in_seconds": APPROVAL_TTL_SECONDS,
            },
        )
        log.info("approval requested", order_id=order.id, symbol=order.symbol, side=order.side)
        return entry

    def resolve(self, order_id: str, *, approved: bool, resolver: str = "human") -> PendingApproval:
        entry = self._pending.get(order_id)
        if entry is None:
            raise KeyError(f"no pending approval for order {order_id}")
        if entry.resolved.is_set():
            raise ValueError(f"approval for {order_id} already resolved")
        entry.approved = approved and not entry.expired
        entry.resolver = resolver
        entry.resolved.set()
        return entry

    async def wait(self, entry: PendingApproval) -> bool:
        """Block until resolved or expired. Returns final approval status."""
        try:
            await asyncio.wait_for(entry.resolved.wait(), timeout=entry.seconds_remaining)
        except TimeoutError:
            entry.approved = False
            entry.resolver = "expired"
            entry.resolved.set()
        finally:
            self._pending.pop(entry.order.id, None)
        return bool(entry.approved)

    def pending(self) -> list[PendingApproval]:
        return [e for e in self._pending.values() if not e.resolved.is_set() and not e.expired]
