from __future__ import annotations

import httpx

from poseidon.cli import probe_model_backend
from poseidon.core.config import AIConfig


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
