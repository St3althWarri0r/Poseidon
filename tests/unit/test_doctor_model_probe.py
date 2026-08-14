"""`poseidon doctor` must not report a model that cannot serve as healthy.

The probe GET `/v1/models`, which on LM Studio lists **downloaded** models, not
**loaded** ones. Observed live during the audit: `openai/gpt-oss-20b` (the
configured model) was present in that listing while another model held the VRAM,
so every `POST /chat/completions` returned HTTP 400 — and doctor printed

    [OK ] model backend reachable (openai_compatible) — reachable at http://localhost:1234/v1

That is a false green on a total loss of function. There is no health probe for
the model either (`_register_probes` covers broker / market_data /
portfolio_sync / holiday_calendar), and a failing cycle produces one deduped
toast per 5 minutes, so doctor is the operator's main signal.

The probe now checks the configured model is actually in the listing. It stays
read-only — a GET, never a completion, never an order.
"""

from __future__ import annotations

import json

import httpx

from poseidon.cli import probe_model_backend
from poseidon.core.config import AIConfig

BASE = "http://localhost:1234/v1"


def _cfg(model: str = "openai/gpt-oss-20b") -> AIConfig:
    return AIConfig(backend="openai_compatible", base_url=BASE, model=model)


def _listing(*ids: str) -> httpx.MockTransport:
    body = {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body))

    return httpx.MockTransport(handler)


def test_configured_model_present_is_ok() -> None:
    ok, detail = probe_model_backend(
        _cfg(), None, transport=_listing("openai/gpt-oss-20b", "other/model")
    )
    assert ok, detail


def test_configured_model_absent_is_not_ok() -> None:
    """The exact live failure: server up, listing served, wrong model."""
    ok, detail = probe_model_backend(
        _cfg(), None, transport=_listing("qwen/qwen3-coder-30b")
    )
    assert not ok, "a backend that cannot serve the configured model is not healthy"
    assert "openai/gpt-oss-20b" in detail
    assert "qwen/qwen3-coder-30b" in detail, "name what IS available so it is actionable"


def test_unreachable_still_says_start_it() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    ok, detail = probe_model_backend(_cfg(), None, transport=httpx.MockTransport(refuse))
    assert not ok
    assert "unreachable" in detail


def test_unparseable_listing_does_not_crash_the_probe() -> None:
    """Degrade to reachability rather than raising — a non-LM-Studio
    OpenAI-compatible server may shape /models differently."""
    def weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="not json")

    ok, detail = probe_model_backend(_cfg(), None, transport=httpx.MockTransport(weird))
    assert ok, detail
