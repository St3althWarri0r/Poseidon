"""Notification service: routes platform events to configured channels.

Subscribes to the event bus and translates the events that matter to a
human (fills, rejections, risk violations, disconnects, approvals, margin
warnings, milestones) into notifications. Deduplicates repeats within a
short window so a flapping component cannot spam every channel.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from ..core.config import NotificationChannelConfig
from ..core.enums import NotificationLevel
from ..core.events import EventBus, Topics
from ..security.vault import Vault
from .channels import CHANNEL_KINDS, Channel

log = structlog.get_logger(__name__)

_DEDUPE_WINDOW = 300.0


class NotificationService:
    def __init__(self, configs: list[NotificationChannelConfig], vault: Vault, bus: EventBus) -> None:
        self._bus = bus
        self._channels: list[Channel] = []
        self._recent: dict[str, float] = {}
        for cfg in configs:
            if not cfg.enabled:
                continue
            cls = CHANNEL_KINDS.get(cfg.kind)
            if cls is None:
                log.error("unknown notification channel kind", kind=cfg.kind)
                continue
            credential: dict[str, str] | None = None
            if cfg.credential:
                credential = vault.get_json(cfg.credential)
            self._channels.append(
                cls(credential=credential, options=cfg.options,
                    min_level=NotificationLevel(cfg.min_level))
            )
        self._wire()

    def _wire(self) -> None:
        bindings = {
            Topics.ORDER_FILLED: self._on_fill,
            Topics.ORDER_REJECTED: self._on_reject,
            Topics.RISK_VIOLATION: self._on_risk,
            Topics.CIRCUIT_OPENED: self._on_circuit,
            Topics.BROKER_DISCONNECTED: self._on_disconnect,
            Topics.BROKER_RECONNECTED: self._on_reconnect,
            Topics.APPROVAL_REQUESTED: self._on_approval,
            Topics.SYSTEM_ERROR: self._on_system_error,
            Topics.NOTIFY: self._on_direct,
        }
        for topic, handler in bindings.items():
            self._bus.subscribe(topic, handler)

    async def notify(self, level: NotificationLevel, title: str, body: str,
                     *, dedupe_key: str | None = None) -> None:
        key = dedupe_key or f"{level}:{title}"
        now = time.monotonic()
        last = self._recent.get(key)
        if last is not None and now - last < _DEDUPE_WINDOW:
            return
        self._recent[key] = now
        if len(self._recent) > 500:
            cutoff = now - _DEDUPE_WINDOW
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}
        targets = [c for c in self._channels if c.accepts(level)]
        if targets:
            results = await asyncio.gather(*(c.send(level, title, body) for c in targets))
            if not any(results) and self._recent.get(key) == now:
                # Every channel failed: forget the dedupe record so a
                # re-published alert (e.g. a repeated CIRCUIT_OPENED) can
                # retry instead of being suppressed for the full window.
                self._recent.pop(key, None)

    # -- event handlers -----------------------------------------------------------

    async def _on_fill(self, _topic: str, payload: Any) -> None:
        order = (payload or {}).get("order", {})
        await self.notify(
            NotificationLevel.INFO, "Order filled",
            f"{order.get('side', '?')} {order.get('filled_quantity')} {order.get('symbol')} "
            f"@ {order.get('avg_fill_price')} via {order.get('broker')}",
            dedupe_key=f"fill:{order.get('id')}",
        )

    async def _on_reject(self, _topic: str, payload: Any) -> None:
        order = (payload or {}).get("order", {})
        await self.notify(
            NotificationLevel.WARNING, "Order rejected",
            f"{order.get('side', '?')} {order.get('quantity')} {order.get('symbol')}: "
            f"{(payload or {}).get('reason', 'unknown')}",
            dedupe_key=f"reject:{order.get('id')}",
        )

    async def _on_risk(self, _topic: str, payload: Any) -> None:
        payload = payload or {}
        await self.notify(
            NotificationLevel.WARNING, f"Risk violation: {payload.get('rule')}",
            f"{payload.get('symbol', '')} — {payload.get('detail', '')}",
            # Include the symbol so a second symbol breaching the SAME rule is
            # not suppressed by the dedupe window (rule+title alone collapsed
            # distinct-symbol alerts).
            dedupe_key=f"risk:{payload.get('rule')}:{payload.get('symbol', '')}",
        )

    async def _on_circuit(self, _topic: str, payload: Any) -> None:
        await self.notify(
            NotificationLevel.CRITICAL, "Circuit breaker opened",
            f"Trading halted: {(payload or {}).get('reason', 'unknown')}",
        )

    async def _on_disconnect(self, _topic: str, payload: Any) -> None:
        payload = payload or {}
        await self.notify(
            NotificationLevel.CRITICAL, "Broker disconnected",
            f"{payload.get('broker')}: {payload.get('error', '')} — sync retrying with backoff",
            dedupe_key=f"disconnect:{payload.get('broker')}",
        )

    async def _on_reconnect(self, _topic: str, payload: Any) -> None:
        await self.notify(
            NotificationLevel.INFO, "Broker reconnected",
            f"{(payload or {}).get('broker')} sync restored",
            dedupe_key=f"reconnect:{(payload or {}).get('broker')}",
        )

    async def _on_approval(self, _topic: str, payload: Any) -> None:
        payload = payload or {}
        order = payload.get("order", {})
        rationale = payload.get("rationale") or {}
        await self.notify(
            NotificationLevel.WARNING, "Trade awaiting your approval",
            f"{order.get('side')} {order.get('quantity')} {order.get('symbol')} "
            f"@ {order.get('limit_price') or 'market'}\n"
            f"Thesis: {rationale.get('thesis', 'n/a')}\n"
            f"Confidence: {rationale.get('confidence', '?')} — approve in the dashboard "
            f"within {int(payload.get('expires_in_seconds', 900)) // 60} minutes.",
            dedupe_key=f"approval:{order.get('id')}",
        )

    async def _on_system_error(self, _topic: str, payload: Any) -> None:
        payload = payload or {}
        await self.notify(
            NotificationLevel.WARNING, f"Component error: {payload.get('component')}",
            str(payload.get("error", "")),
            dedupe_key=f"syserr:{payload.get('component')}",
        )

    async def _on_direct(self, _topic: str, payload: Any) -> None:
        payload = payload or {}
        await self.notify(
            NotificationLevel(payload.get("level", "info")),
            payload.get("title", "Poseidon"), payload.get("body", ""),
        )
