# tests/unit/test_strategy_health_wiring.py
from __future__ import annotations

from types import SimpleNamespace

from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig
from poseidon.security.vault import Vault


def test_default_schedule_only_when_enabled(tmp_path) -> None:
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    scheds = kernel._effective_schedules()
    assert any(s.job == "strategy_health_sweep" for s in scheds)          # default present
    off = ApplicationKernel(
        AppConfig(strategy_health={"enabled": False}), Vault(tmp_path / "v2.bin"))
    assert not any(s.job == "strategy_health_sweep" for s in off._effective_schedules())


def test_retire_adapter_is_reduce_only_for_unknown(tmp_path) -> None:
    # a strategy with no active custom algorithm -> adapter returns False (flag-only), never raises
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    kernel.workshop = SimpleNamespace(list_all=_none, deactivate=_boom)   # deactivate must not run
    import asyncio
    assert asyncio.run(kernel._retire_strategy("nonexistent")) is False


async def _none() -> list:
    return []


async def _boom(*a, **k):
    raise AssertionError("deactivate must not be called for an unknown/builtin strategy")
