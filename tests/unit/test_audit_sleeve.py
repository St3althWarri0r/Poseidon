# tests/unit/test_audit_sleeve.py
"""Auto-retire must not pop a strategy's sleeve cap out of the live RiskEngine
dict while a review cycle is in flight: the cycle's attributed order would then
validate against the looser generic max_position_pct instead of the operator's
tighter sleeve. _retire_strategy serializes the workshop.deactivate mutation
against the kernel cycle lock so the pop can only land between cycles."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig
from poseidon.security.vault import Vault

SLEEVE = "algo:momo"


def _sleeve_popping_workshop(sleeve_caps: dict[str, float]) -> SimpleNamespace:
    """Workshop double that mirrors the real deactivate side effect: popping
    the strategy's entry from the (shared, live) sleeve-cap dict."""

    async def _list_all() -> list[dict[str, object]]:
        return [{"name": "momo", "status": "active", "id": "a1"}]

    async def _deactivate(algo_id: str, *, archive: bool = False,
                          actor: str = "human") -> dict[str, object]:
        sleeve_caps.pop(SLEEVE, None)
        return {}

    return SimpleNamespace(list_all=_list_all, deactivate=_deactivate)


async def test_retire_blocks_on_in_flight_cycle_and_completes_after(tmp_path) -> None:
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    sleeve_caps = {SLEEVE: 0.01}
    kernel.workshop = _sleeve_popping_workshop(sleeve_caps)  # type: ignore[assignment]

    # Simulate an in-flight review cycle: run_review_cycle holds _cycle_lock
    # from set_cycle_attribution through execute_decision.
    await kernel._cycle_lock.acquire()
    try:
        retire = asyncio.create_task(kernel._retire_strategy(SLEEVE))
        # Give the retire task ample turns to run as far as it can.
        for _ in range(10):
            await asyncio.sleep(0)
        assert not retire.done(), "retire must block while a cycle is in flight"
        assert sleeve_caps == {SLEEVE: 0.01}, (
            "sleeve cap must stay intact for the duration of the cycle")
    finally:
        kernel._cycle_lock.release()

    # Once the cycle releases the lock, the retire proceeds and pops the sleeve.
    assert await asyncio.wait_for(retire, timeout=1.0) is True
    assert SLEEVE not in sleeve_caps


async def test_retire_no_op_does_not_touch_cycle_lock(tmp_path) -> None:
    # Reduce-only miss (no matching ACTIVE custom algorithm): nothing to
    # serialize, so an in-flight cycle must not delay the False return.
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    kernel.workshop = _sleeve_popping_workshop({})  # type: ignore[assignment]
    await kernel._cycle_lock.acquire()
    try:
        assert await asyncio.wait_for(
            kernel._retire_strategy("algo:missing"), timeout=1.0) is False
    finally:
        kernel._cycle_lock.release()
