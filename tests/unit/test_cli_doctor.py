from __future__ import annotations

import argparse

import httpx

from poseidon import cli
from poseidon.cli import probe_model_backend
from poseidon.core.config import AIConfig, AppConfig


def _openai_cfg() -> AIConfig:
    return AIConfig(backend="openai_compatible", base_url="http://localhost:1234/v1",
                    model="devstral")


def test_openai_backend_reachable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "http://localhost:1234/v1/models"
        return httpx.Response(200, json={"data": []})

    ok, detail = probe_model_backend(_openai_cfg(), None,
                                     transport=httpx.MockTransport(handler))
    assert ok is True
    assert "reachable at http://localhost:1234/v1" in detail


def test_openai_backend_connect_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    ok, detail = probe_model_backend(_openai_cfg(), None,
                                     transport=httpx.MockTransport(handler))
    assert ok is False
    assert "http://localhost:1234/v1" in detail
    assert "LM Studio" in detail


def test_openai_backend_http_5xx_reports_reachable_not_start() -> None:
    # A running LM Studio that returns 500 is UP — the probe must say so, not
    # tell the user to start a server that is already running.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    ok, detail = probe_model_backend(_openai_cfg(), None,
                                     transport=httpx.MockTransport(handler))
    assert ok is False
    assert "500" in detail
    assert "reachable" in detail
    assert "start LM Studio" not in detail


def test_anthropic_key_rejected() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://api.anthropic.com/v1/models"
        return httpx.Response(401, json={"error": "unauthorized"})

    ok, detail = probe_model_backend(AIConfig(), "bad-key",
                                     transport=httpx.MockTransport(handler))
    assert ok is False
    assert "key rejected" in detail


def test_anthropic_reachable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    ok, detail = probe_model_backend(AIConfig(), "good-key",
                                     transport=httpx.MockTransport(handler))
    assert ok is True
    assert "Anthropic API reachable" in detail


def test_anthropic_connect_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ok, detail = probe_model_backend(AIConfig(), "good-key",
                                     transport=httpx.MockTransport(handler))
    assert ok is False
    assert "cannot reach Anthropic API" in detail


# ---------------------------------------------------------------- cmd_doctor wiring


class _FakeVault:
    """Minimal vault stand-in for cmd_doctor: exists, unlocks, holds names."""

    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = dict(entries)
        self.exists = True

    def names(self) -> list[str]:
        return list(self._entries)

    def get(self, name: str) -> str:
        return self._entries[name]


def _run_doctor(monkeypatch, tmp_path, config: AppConfig,
                entries: dict[str, str]) -> tuple[int, dict[str, object]]:
    """Drive cmd_doctor with a fake vault + a probe spy, no network/real vault."""
    captured: dict[str, object] = {}

    def fake_probe(ai, api_key, *, transport=None):  # type: ignore[no-untyped-def]
        captured["ai"] = ai
        captured["api_key"] = api_key
        return True, "spy-probe-detail"

    monkeypatch.setattr(cli, "probe_model_backend", fake_probe)
    monkeypatch.setattr(cli, "_load", lambda args: config)
    monkeypatch.setattr(cli, "_vault_for", lambda cfg: _FakeVault(entries))
    monkeypatch.setattr(cli, "_unlock", lambda vault, **kw: None)
    rc = cli.cmd_doctor(argparse.Namespace(config=None))
    return rc, captured


def test_cmd_doctor_openai_backend_renders_line_and_passes_no_key(
    monkeypatch, tmp_path, capsys
) -> None:
    config = AppConfig(
        data_dir=tmp_path,
        ai=AIConfig(backend="openai_compatible",
                    base_url="http://localhost:1234/v1", model="devstral"),
    )
    _, captured = _run_doctor(
        monkeypatch, tmp_path, config,
        {config.ai.api_key_credential: "MUST_NOT_BE_PASSED"},
    )
    out = capsys.readouterr().out
    # the reachability line renders with the probe's detail
    assert "model backend reachable (openai_compatible)" in out
    assert "spy-probe-detail" in out
    # key_or_none is None for a non-anthropic backend
    assert captured["api_key"] is None
    assert captured["ai"] is config.ai


def test_cmd_doctor_anthropic_backend_passes_stored_key(
    monkeypatch, tmp_path, capsys
) -> None:
    config = AppConfig(data_dir=tmp_path)  # default backend == "anthropic"
    assert config.ai.backend == "anthropic"
    _, captured = _run_doctor(
        monkeypatch, tmp_path, config,
        {config.ai.api_key_credential: "the-anthropic-key"},
    )
    out = capsys.readouterr().out
    assert "model backend reachable (anthropic)" in out
    # anthropic backend: the stored key IS passed through to the probe
    assert captured["api_key"] == "the-anthropic-key"
