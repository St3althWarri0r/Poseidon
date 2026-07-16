"""Database.prune_advisory (added in the audit-fix pass) deletes stale
trade_lessons/analysis_packets rows but nothing calls it in production. This
wires a nightly sweep job, mirroring test_analysis_schedule.py's
_effective_schedules() cadence check and test_strategy_health_wiring.py's
pattern of calling a kernel job method directly against a stubbed
dependency (no full start()).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import structlog.testing

from poseidon.app import ApplicationKernel
from poseidon.core.config import (
    AIConfig,
    AnalysisConfig,
    AppConfig,
    ReflectionConfig,
    ScheduleConfig,
)
from poseidon.scheduler.scheduler import Scheduler
from poseidon.security.vault import Vault


def _kernel(tmp_path, *, lookback_days: int = 45, refresh_hours: int = 12) -> ApplicationKernel:
    cfg = AppConfig(ai=AIConfig(
        reflection=ReflectionConfig(lookback_days=lookback_days),
        analysis=AnalysisConfig(refresh_hours=refresh_hours),
    ))
    return ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))


# ------------------------------------------------------------- cadence


def test_advisory_prune_gets_a_default_nightly_schedule(tmp_path) -> None:
    kernel = _kernel(tmp_path)
    schedules = kernel._effective_schedules()
    matches = [s for s in schedules if s.job == "advisory_prune"]
    assert len(matches) == 1
    assert matches[0].cron is not None  # cron cadence (daily), not every_seconds


def test_an_explicit_user_schedule_is_not_duplicated(tmp_path) -> None:
    # Mirrors the analysis_sweep guard: an operator-defined schedule for the
    # same job must not get a second default appended alongside it.
    cfg = AppConfig(schedules=[
        ScheduleConfig(name="my-prune", job="advisory_prune", cron="0 3 * * *"),
    ])
    kernel = ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))
    schedules = kernel._effective_schedules()
    matches = [s for s in schedules if s.job == "advisory_prune"]
    assert len(matches) == 1
    assert matches[0].name == "my-prune"


# ---------------------------------------------------------- job registration


async def _noop() -> None:
    return None


def test_advisory_prune_job_is_registered(tmp_path) -> None:
    kernel = _kernel(tmp_path)
    kernel.scheduler = Scheduler(kernel.clock, kernel.bus)
    # _register_jobs dereferences .sync_once/.check_all eagerly; analysis and
    # strategy_health are already None from __init__ so their conditional
    # registrations are skipped.
    kernel.sync = SimpleNamespace(sync_once=_noop)  # type: ignore[assignment]
    kernel.guardian = SimpleNamespace(check_all=_noop)  # type: ignore[assignment]
    kernel._register_jobs()
    assert "advisory_prune" in kernel.scheduler._jobs


# ------------------------------------------------------------ job behavior


def _fake_db(calls: list[dict[str, object]]) -> SimpleNamespace:
    async def _prune_advisory(*, lesson_lookback_days: int, packet_refresh_hours: int,
                              now: datetime) -> tuple[int, int]:
        calls.append({
            "lesson_lookback_days": lesson_lookback_days,
            "packet_refresh_hours": packet_refresh_hours,
            "now": now,
        })
        return (3, 5)
    return SimpleNamespace(prune_advisory=_prune_advisory)


async def test_advisory_prune_job_calls_prune_advisory_with_config_derived_args(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    # Non-default values: proves the config path is actually read, not a
    # hardcoded int that happens to match ReflectionConfig/AnalysisConfig defaults.
    kernel = _kernel(tmp_path, lookback_days=45, refresh_hours=12)
    kernel.db = _fake_db(calls)  # type: ignore[assignment]

    with structlog.testing.capture_logs() as logs:
        await kernel._advisory_prune_job()

    assert len(calls) == 1
    assert calls[0]["lesson_lookback_days"] == 45
    assert calls[0]["packet_refresh_hours"] == 12
    assert isinstance(calls[0]["now"], datetime)

    prune_logs = [e for e in logs if e.get("event") == "advisory_prune"]
    assert len(prune_logs) == 1
    assert prune_logs[0]["lessons"] == 3
    assert prune_logs[0]["packets"] == 5
