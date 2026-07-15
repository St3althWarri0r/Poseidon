# tests/unit/test_strategy_health_wiring.py
from __future__ import annotations

import asyncio
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


def _tracking_workshop(algos: list[dict[str, object]],
                       calls: list[dict[str, object]]) -> SimpleNamespace:
    """A workshop double whose deactivate call-TRACKING fake records exactly
    what it was invoked with (not an in-band raise, which would be silently
    swallowed by _retire_strategy's try/except and prove nothing)."""

    async def _list_all() -> list[dict[str, object]]:
        return algos

    async def _deactivate(algo_id: str, *, archive: bool = False,
                          actor: str = "human") -> dict[str, object]:
        calls.append({"algo_id": algo_id, "archive": archive, "actor": actor})
        return {}

    return SimpleNamespace(list_all=_list_all, deactivate=_deactivate)


def test_retire_adapter_is_reduce_only_for_unknown(tmp_path) -> None:
    # No ACTIVE custom algorithm named "missing" -> adapter returns False
    # (flag-only) and deactivate must never be called.
    calls: list[dict[str, object]] = []
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    kernel.workshop = _tracking_workshop(
        [{"name": "other", "status": "active", "id": "a1"}], calls)
    assert asyncio.run(kernel._retire_strategy("algo:missing")) is False
    assert calls == []


def test_retire_adapter_deactivates_matching_active_custom_algorithm(tmp_path) -> None:
    # The bug: trades are attributed as RoundTrip.strategy == "algo:<name>", but
    # the workshop stores the bare <name>. _retire_strategy must match the
    # prefixed form against the bare workshop name, and must mark the
    # deactivation as system- (not human-) initiated since the watchdog is
    # autonomous.
    calls: list[dict[str, object]] = []
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    kernel.workshop = _tracking_workshop(
        [{"name": "momo", "status": "active", "id": "a1"}], calls)
    assert asyncio.run(kernel._retire_strategy("algo:momo")) is True
    assert calls == [{"algo_id": "a1", "archive": False, "actor": "system"}]
