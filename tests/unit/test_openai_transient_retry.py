"""One bad generation must not cost a whole review cycle.

LM Studio + gpt-oss-20b intermittently fails to parse its own model's output:

    HTTP 400 — {"error":"Engine protocol predict stream returned an error:
      {\\"code\\":500,\\"message\\":\\"The model produced output that does not
       match the expected peg-native format\\",\\"type\\":\\"server_error\\"}"}

Measured on the operator's running system: **70 failures against 1074 completed
cycles, ~6%.** It is not caused by anything in the request — the same prompt,
tool catalog, `strict` setting, `max_tokens` and multi-turn depth all succeed on
retry, and none of them reproduces it deterministically. It is the model
producing output its own runtime cannot parse.

Poseidon cannot prevent that. What it should not do is throw away the cycle:
before this, one bad generation aborted the cycle, published SYSTEM_ERROR, and
skipped that 60s slot entirely.

Retried ONCE, and only for this transient class. Deliberately NOT retried:

* context overflow — deterministic, a retry burns time and fails identically,
  and the operator needs the remedy message instead;
* auth and model-not-found — configuration, not luck.

Retrying is safe here specifically because a completion places no order: the
order path is downstream of `submit_decision`, so a repeated request cannot
double-execute anything.
"""

from __future__ import annotations

import httpx
import pytest

from poseidon.ai.backends.openai_backend import OpenAICompatibleBackend
from poseidon.core.config import AIConfig
from poseidon.core.errors import AgentError

PEG = ('{"error":"Engine protocol predict stream returned an error: '
       '{\\"code\\":500,\\"message\\":\\"The model produced output that does not '
       'match the expected peg-native format\\",\\"type\\":\\"server_error\\"}"}')
OVERFLOW = '{"error":{"type":"exceed_context_size_error","message":"prompt is too long"}}'

GOOD = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}


def _backend(responses: list[httpx.Response]) -> tuple[OpenAICompatibleBackend, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    cfg = AIConfig(backend="openai_compatible", base_url="http://x/v1", model="m")
    return OpenAICompatibleBackend(cfg, transport=httpx.MockTransport(handler)), calls


async def test_a_transient_generation_failure_is_retried_once() -> None:
    backend, calls = _backend([httpx.Response(400, text=PEG), httpx.Response(200, json=GOOD)])
    resp = await backend.complete([{"role": "user", "content": "hi"}], tools=[], system="s")
    assert resp.text == "ok"
    assert len(calls) == 2, "the transient failure should have been retried exactly once"


async def test_it_gives_up_after_one_retry() -> None:
    backend, calls = _backend([httpx.Response(400, text=PEG)])
    with pytest.raises(AgentError, match="peg-native|predict stream"):
        await backend.complete([{"role": "user", "content": "hi"}], tools=[], system="s")
    assert len(calls) == 2, "must not retry forever on a persistently broken model"


async def test_context_overflow_is_not_retried() -> None:
    """Deterministic: a retry wastes a cycle and buries the remedy message."""
    backend, calls = _backend([httpx.Response(400, text=OVERFLOW)])
    with pytest.raises(AgentError, match="context length"):
        await backend.complete([{"role": "user", "content": "hi"}], tools=[], system="s")
    assert len(calls) == 1


async def test_auth_failure_is_not_retried() -> None:
    backend, calls = _backend([httpx.Response(401, text='{"error":"bad key"}')])
    with pytest.raises(AgentError):
        await backend.complete([{"role": "user", "content": "hi"}], tools=[], system="s")
    assert len(calls) == 1


async def test_a_clean_response_is_not_retried() -> None:
    backend, calls = _backend([httpx.Response(200, json=GOOD)])
    await backend.complete([{"role": "user", "content": "hi"}], tools=[], system="s")
    assert len(calls) == 1
