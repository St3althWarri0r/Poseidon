"""Unit tests for ai/hardware.py: probe_local_models + detect_vram + vram_fit_hint.

All three degrade to a safe value and never raise: the probe returns
``(False, [])`` on any transport/shape error, ``detect_vram`` returns ``None``
when no GPU tool is present or its output cannot be parsed, and the hint is a
pure string heuristic. Tests inject a fake httpx transport and a fake
subprocess runner so nothing touches a real endpoint or GPU.
"""
from __future__ import annotations

import subprocess

import httpx
import pytest

from poseidon.ai import hardware
from poseidon.ai.hardware import (
    detect_vram,
    probe_local_models,
    vram_fit_hint,
)

BASE_URL = "http://localhost:1234/v1"


# --- probe_local_models ------------------------------------------------------

async def test_probe_reachable_returns_model_ids() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"data": [
            {"id": "openai/gpt-oss-20b"}, {"id": "qwen/qwen3-coder-30b"}]})

    reachable, models = await probe_local_models(
        BASE_URL, transport=httpx.MockTransport(handler))

    assert reachable is True
    assert models == ["openai/gpt-oss-20b", "qwen/qwen3-coder-30b"]
    assert captured["url"] == "http://localhost:1234/v1/models"


async def test_probe_http_error_returns_false_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    reachable, models = await probe_local_models(
        BASE_URL, transport=httpx.MockTransport(handler))

    assert reachable is False
    assert models == []


async def test_probe_connect_error_returns_false_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    reachable, models = await probe_local_models(
        BASE_URL, transport=httpx.MockTransport(handler))

    assert reachable is False
    assert models == []


async def test_probe_malformed_shape_returns_false_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # data entries missing "id" -> KeyError-shaped failure, swallowed.
        return httpx.Response(200, json={"data": [{"name": "x"}]})

    reachable, models = await probe_local_models(
        BASE_URL, transport=httpx.MockTransport(handler))

    assert reachable is False
    assert models == []


async def test_probe_strips_trailing_slash() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"data": []})

    reachable, models = await probe_local_models(
        BASE_URL + "/", transport=httpx.MockTransport(handler))

    assert reachable is True
    assert models == []
    assert captured["url"] == "http://localhost:1234/v1/models"


# --- detect_vram -------------------------------------------------------------

async def test_detect_vram_parses_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "nvidia-smi"
        return subprocess.CompletedProcess(cmd, 0, stdout="16384, 11469\n", stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    result = await detect_vram()

    assert result is not None
    assert result["total_gb"] == pytest.approx(16.0, abs=0.1)
    assert result["free_gb"] == pytest.approx(11.2, abs=0.1)


async def test_detect_vram_falls_back_to_rocm(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "nvidia-smi":
            raise FileNotFoundError("no nvidia-smi")
        assert cmd[0] == "rocm-smi"
        out = ("GPU[0]\t\t: VRAM Total Memory (B): 17179869184\n"
               "GPU[0]\t\t: VRAM Total Used Memory (B): 2147483648\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    result = await detect_vram()

    assert result is not None
    assert result["total_gb"] == pytest.approx(16.0, abs=0.1)
    assert result["free_gb"] == pytest.approx(14.0, abs=0.1)


async def test_detect_vram_none_when_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert await detect_vram() is None


async def test_detect_vram_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert await detect_vram() is None


async def test_detect_vram_none_on_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="garbage output\n", stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert await detect_vram() is None


async def test_detect_vram_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 2.0)

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert await detect_vram() is None


# --- vram_fit_hint -----------------------------------------------------------

def test_vram_fit_hint_16gb() -> None:
    hint = vram_fit_hint(16.0)
    assert "20B" in hint
    assert "30B" in hint


def test_vram_fit_hint_is_string_for_small_vram() -> None:
    hint = vram_fit_hint(4.0)
    assert isinstance(hint, str)
    assert hint  # non-empty
