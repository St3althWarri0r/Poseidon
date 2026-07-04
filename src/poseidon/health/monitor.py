"""Health monitor and watchdog.

Every subsystem registers a probe. The watchdog loop runs them on an
interval, tracks state transitions, publishes health-change events (which
the notifier escalates), and exposes the aggregate for the dashboard and
the ``poseidon doctor`` self-diagnostics command. systemd's watchdog is fed
from the same loop (via sd_notify) so a hung process gets restarted.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog

from ..core.enums import HealthState
from ..core.events import EventBus, Topics
from ..core.models import ComponentHealth

log = structlog.get_logger(__name__)

Probe = Callable[[], Awaitable[tuple[HealthState, str | None]]]


def _sd_notify(message: str) -> None:
    """Best-effort systemd notification (WATCHDOG=1 / READY=1)."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    try:
        if path.startswith("@"):
            path = "\0" + path[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.send(message.encode())
    except OSError:
        pass


class HealthMonitor:
    def __init__(self, bus: EventBus, *, interval_seconds: float = 30.0) -> None:
        self._bus = bus
        self._interval = interval_seconds
        self._probes: dict[str, Probe] = {}
        self._results: dict[str, ComponentHealth] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def register(self, name: str, probe: Probe) -> None:
        self._probes[name] = probe

    async def start(self) -> None:
        _sd_notify("READY=1")
        self._task = asyncio.create_task(self._loop(), name="health-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.run_probes()
            _sd_notify("WATCHDOG=1")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def run_probes(self) -> dict[str, ComponentHealth]:
        for name, probe in self._probes.items():
            started = time.monotonic()
            try:
                state, detail = await asyncio.wait_for(probe(), timeout=20)
            except TimeoutError:
                state, detail = HealthState.UNHEALTHY, "probe timed out"
            except Exception as exc:
                state, detail = HealthState.UNHEALTHY, f"probe error: {exc}"
            latency = (time.monotonic() - started) * 1000
            previous = self._results.get(name)
            result = ComponentHealth(
                name=name, state=state.value, detail=detail,
                latency_ms=round(latency, 1), checked_at=datetime.now(UTC),
            )
            self._results[name] = result
            previous_state = previous.state if previous is not None else HealthState.HEALTHY.value
            if previous_state != result.state:
                log.info("health transition", component=name,
                         was=previous_state, now=result.state, detail=detail)
                await self._bus.publish(Topics.HEALTH_CHANGED, result.model_dump(mode="json"))
        return self._results

    @property
    def overall(self) -> HealthState:
        states = {r.state for r in self._results.values()}
        if HealthState.UNHEALTHY.value in states:
            return HealthState.UNHEALTHY
        if HealthState.DEGRADED.value in states:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    def report(self) -> dict[str, object]:
        return {
            "overall": self.overall.value,
            "components": {n: r.model_dump(mode="json") for n, r in self._results.items()},
        }
