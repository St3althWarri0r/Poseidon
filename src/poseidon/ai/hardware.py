"""Best-effort local-hardware probes for the model selector (dependency-free).

Three helpers, none of which ever raises:

* :func:`probe_local_models` — is an LM Studio / OpenAI-compatible endpoint up,
  and which model ids does it advertise? Used by ``GET /api/models`` and by the
  apply-config precondition so Poseidon never switches into a dead backend.
* :func:`detect_vram` — how much GPU memory is present (a *hint only*, never
  enforced). Shells out to ``nvidia-smi`` then ``rocm-smi``; ``None`` when no
  tool is present or its output cannot be parsed.
* :func:`vram_fit_hint` — a rough, explicitly-approximate string mapping total
  VRAM to a comfortable Q4 model size.

Everything degrades to a safe value so the dashboard renders even with no GPU
and no local server running.
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx

# Default local endpoint when ai.base_url is unset (config.base_url defaults to
# None). Kept here so both the probe callers and the config/apply path share one
# source of truth.
DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"

_PROBE_TIMEOUT_S = 3.0
_SMI_TIMEOUT_S = 2.0
# Q4_K_M weights ~= params_B * 0.6 GB; ~2 GB runtime/KV-cache overhead.
_GB_PER_B = 0.6
_OVERHEAD_GB = 2.0
_MIB_PER_GB = 1024.0


async def probe_local_models(
    base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, list[str]]:
    """``(reachable, model_ids)`` for an OpenAI-compatible ``/models`` endpoint.

    Short-timeout GET so a down server fails fast. Any transport error, non-2xx
    status, or unexpected JSON shape degrades to ``(False, [])`` — the caller
    treats that as "local backend unavailable". ``transport`` is injectable so
    tests can feed a fake without a live endpoint.
    """
    client = httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(_PROBE_TIMEOUT_S, connect=_PROBE_TIMEOUT_S),
        transport=transport,
    )
    try:
        r = await client.get("/models")
        r.raise_for_status()
        data = r.json()
        models = [str(m["id"]) for m in data["data"]]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return False, []
    finally:
        await client.aclose()
    return True, models


async def detect_vram() -> dict[str, float] | None:
    """Total (and, when available, free) GPU memory in GB, or ``None``.

    Best-effort: tries ``nvidia-smi`` then ``rocm-smi`` off the event loop.
    ``None`` on a missing binary, nonzero exit, timeout, or unparseable output.
    Never raises — VRAM is a display hint, not a gate.
    """
    return await asyncio.to_thread(_detect_vram_sync)


def _detect_vram_sync() -> dict[str, float] | None:
    for query in (_query_nvidia, _query_rocm):
        result = query()
        if result is not None:
            return result
    return None


def _run(cmd: list[str]) -> str | None:
    """Run a probe command, returning stdout on exit 0 or ``None`` on any
    failure (missing binary, nonzero exit, timeout)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SMI_TIMEOUT_S,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _query_nvidia() -> dict[str, float] | None:
    out = _run([
        "nvidia-smi",
        "--query-gpu=memory.total,memory.free",
        "--format=csv,noheader,nounits",
    ])
    if out is None:
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None
    try:
        total_mib = float(parts[0])
        free_mib = float(parts[1])
    except ValueError:
        return None
    return {"total_gb": round(total_mib / _MIB_PER_GB, 1),
            "free_gb": round(free_mib / _MIB_PER_GB, 1)}


def _query_rocm() -> dict[str, float] | None:
    out = _run(["rocm-smi", "--showmeminfo", "vram"])
    if out is None:
        return None
    total_b: float | None = None
    used_b: float | None = None
    for raw in out.splitlines():
        low = raw.lower()
        value = _trailing_int(raw)
        if value is None:
            continue
        if "total memory" in low:
            total_b = value
        elif "used memory" in low:
            used_b = value
    if total_b is None:
        return None
    result = {"total_gb": round(total_b / 1024**3, 1)}
    if used_b is not None:
        result["free_gb"] = round((total_b - used_b) / 1024**3, 1)
    return result


def _trailing_int(line: str) -> float | None:
    """Parse the integer after the final ``:`` in a rocm-smi line."""
    if ":" not in line:
        return None
    tail = line.rsplit(":", 1)[1].strip()
    try:
        return float(tail)
    except ValueError:
        return None


def vram_fit_hint(total_gb: float) -> str:
    """A rough, explicitly-approximate sizing hint for a given total VRAM.

    Q4_K_M weights run ~``params_B * 0.6`` GB plus ~2 GB overhead, so the
    largest comfortable model is ``(total - 2) / 0.6`` billion params, floored
    to a round 5B tier; the next ~10B up is called out as tight. Copy is
    deliberately fuzzy — this never gates anything.
    """
    raw_b = (total_gb - _OVERHEAD_GB) / _GB_PER_B
    comfortable = max(int(raw_b) // 5 * 5, 0)
    tight = comfortable + 10
    return (f"~{comfortable}B (Q4) fit comfortably; ~{tight}B is tight.")
