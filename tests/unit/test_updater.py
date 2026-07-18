from __future__ import annotations

import structlog.testing

from poseidon.core.config import UpdateConfig
from poseidon.core.events import EventBus, Topics
from poseidon.updater import UpdateService


def test_auto_apply_default_on() -> None:
    """Fresh installs self-update on launch out of the box (restart-gated)."""
    assert UpdateConfig().auto_apply is True


def test_auto_apply_still_overridable() -> None:
    assert UpdateConfig(auto_apply=False).auto_apply is False


async def test_apply_success_notifies_and_warns(monkeypatch, tmp_path) -> None:
    """A successful apply keeps the desktop toast AND logs a WARNING so the
    restart signal is visible in the terminal/journal for `poseidon run`."""
    (tmp_path / ".git").mkdir()
    bus = EventBus()
    notes: list[dict[str, object]] = []

    async def _capture(_topic: str, payload: dict[str, object]) -> None:
        notes.append(payload)

    bus.subscribe(Topics.NOTIFY, _capture)

    svc = UpdateService(UpdateConfig(), bus, repo_dir=tmp_path)
    svc.available = "abc123def456"

    async def _fake_run(cmd: list[str]) -> tuple[int, str]:
        return 0, ""

    monkeypatch.setattr(svc, "_run", _fake_run)

    with structlog.testing.capture_logs() as logs:
        ok = await svc._apply()
    await bus.close()

    assert ok is True
    warnings = [
        log
        for log in logs
        if log.get("log_level") == "warning"
        and log["event"] == "update installed — restart to activate"
    ]
    assert warnings, "expected a WARNING restart signal in the logs"
    assert warnings[0]["restart_cmd"] == "systemctl --user restart poseidon"
    assert warnings[0]["remote"] == "abc123def456"

    # The desktop toast is still emitted alongside the log.
    assert any(n.get("title") == "Update applied" for n in notes)


async def test_apply_failure_never_warns_restart(monkeypatch, tmp_path) -> None:
    """A failed git pull must not emit the restart-to-activate signal."""
    (tmp_path / ".git").mkdir()
    bus = EventBus()
    svc = UpdateService(UpdateConfig(), bus, repo_dir=tmp_path)

    async def _fail_run(cmd: list[str]) -> tuple[int, str]:
        return 1, "diverged"

    monkeypatch.setattr(svc, "_run", _fail_run)

    with structlog.testing.capture_logs() as logs:
        ok = await svc._apply()
    await bus.close()

    assert ok is False
    assert not [
        log
        for log in logs
        if log["event"] == "update installed — restart to activate"
    ]
