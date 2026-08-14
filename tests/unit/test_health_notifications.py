"""A health transition must reach the operator.

``HealthMonitor`` publishes ``Topics.HEALTH_CHANGED`` on every component state
transition, and ``health/monitor.py``'s module docstring says those events are
the ones "the notifier escalates". They were not: an exhaustive grep of every
``.subscribe(`` call showed **zero** subscribers to that topic. So a broker ping
failing, every data provider entering the penalty box, or portfolio sync going
stale produced a log line and nothing else — while the risk engine had already
stopped accepting orders (``FreshPortfolioRule`` refuses at 120s, and it does
not exempt exits).

Health was pull-only via ``GET /api/status``, which requires a human to be
looking at the dashboard — the one thing that is not true at 3am.
"""

from __future__ import annotations

from pathlib import Path

from poseidon.core.enums import NotificationLevel
from poseidon.core.events import EventBus, Topics
from poseidon.notifications.service import NotificationService
from poseidon.security.vault import Vault


class _Recorder:
    kind = "recorder"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def accepts(self, _level: NotificationLevel) -> bool:
        return True

    async def send(self, level: NotificationLevel, title: str, body: str) -> bool:
        self.sent.append((level.value, title, body))
        return True


async def _service_with_recorder(tmp_path: object) -> tuple[EventBus, _Recorder]:
    """Real NotificationService (so the real _wire bindings are exercised) with
    no configured channels, then a recorder injected as the only sink."""
    bus = EventBus()
    # No channel configs -> the vault is never consulted, so an unopened one is
    # fine and no credential is touched.
    svc = NotificationService([], Vault(Path(str(tmp_path)) / "v.bin"), bus)
    rec = _Recorder()
    svc._channels = [rec]  # type: ignore[list-item]  # noqa: SLF001
    return bus, rec


async def _publish(bus: EventBus, name: str, state: str, detail: str = "") -> None:
    await bus.publish(Topics.HEALTH_CHANGED,
                      {"name": name, "state": state, "detail": detail,
                       "latency_ms": 1.0, "checked_at": "2026-08-14T00:00:00+00:00"})
    await bus.close()


async def test_unhealthy_transition_notifies(tmp_path) -> None:
    bus, rec = await _service_with_recorder(tmp_path)
    await _publish(bus, "portfolio_sync", "unhealthy", "state is 600s old")
    assert rec.sent, "a component going UNHEALTHY must reach the operator"
    level, title, body = rec.sent[0]
    assert level == "critical"
    assert "portfolio_sync" in title
    assert "600s" in body, "the probe's own detail is what makes it actionable"


async def test_degraded_transition_notifies_at_warning(tmp_path) -> None:
    bus, rec = await _service_with_recorder(tmp_path)
    await _publish(bus, "market_data", "degraded", "2 providers penalised")
    assert rec.sent
    assert rec.sent[0][0] == "warning"


async def test_recovery_is_reported_too(tmp_path) -> None:
    """Silence after a critical is ambiguous — say when it comes back."""
    bus, rec = await _service_with_recorder(tmp_path)
    await _publish(bus, "broker", "healthy", "reconnected")
    assert rec.sent
    assert rec.sent[0][0] == "info"


async def test_malformed_payload_does_not_raise(tmp_path) -> None:
    """Event handlers must never take down the bus."""
    bus, rec = await _service_with_recorder(tmp_path)
    await bus.publish(Topics.HEALTH_CHANGED, None)
    await bus.publish(Topics.HEALTH_CHANGED, {"unexpected": True})
    await bus.close()
    assert True  # reaching here without an exception is the assertion
