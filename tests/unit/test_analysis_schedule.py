"""The analysis firm is dead unless its sweep actually fires.

``_register_jobs`` registers "analysis_sweep" whenever ``AnalysisService`` is
wired (see app.py), but a *registered* job only runs if the effective schedule
list actually contains it. Config ships with no default schedules for it, so
flipping ``ai.analysis.enabled: true`` alone silently did nothing — the fix
mirrors the existing ``risk_metrics``/``daily_report`` conditional defaults in
``ApplicationKernel._effective_schedules``: add a default cron schedule when
the feature is on and the operator hasn't defined their own.
"""
from __future__ import annotations

from poseidon.app import ApplicationKernel
from poseidon.core.config import AIConfig, AnalysisConfig, AppConfig, ScheduleConfig
from poseidon.security.vault import Vault


def _kernel(*, enabled: bool, tmp_path) -> ApplicationKernel:
    cfg = AppConfig(ai=AIConfig(analysis=AnalysisConfig(enabled=enabled)))
    return ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))


def test_analysis_sweep_gets_a_default_schedule_when_enabled(tmp_path) -> None:
    kernel = _kernel(enabled=True, tmp_path=tmp_path)
    schedules = kernel._effective_schedules()
    assert any(s.job == "analysis_sweep" for s in schedules)


def test_analysis_sweep_has_no_default_schedule_when_disabled(tmp_path) -> None:
    kernel = _kernel(enabled=False, tmp_path=tmp_path)
    schedules = kernel._effective_schedules()
    assert not any(s.job == "analysis_sweep" for s in schedules)


def test_an_explicit_user_schedule_is_not_duplicated(tmp_path) -> None:
    # An operator-defined schedule for the same job must not get a second,
    # redundant default appended alongside it (mirrors the guard used for
    # every other conditional default in _effective_schedules).
    cfg = AppConfig(
        ai=AIConfig(analysis=AnalysisConfig(enabled=True)),
        schedules=[ScheduleConfig(name="my-sweep", job="analysis_sweep",
                                  cron="0 7 * * *")],
    )
    kernel = ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))
    schedules = kernel._effective_schedules()
    matches = [s for s in schedules if s.job == "analysis_sweep"]
    assert len(matches) == 1
    assert matches[0].name == "my-sweep"
