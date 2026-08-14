"""`strict` on an OpenAI-compatible server must be opt-in.

Sending `strict: true` asks the server for grammar-constrained decoding. Not
every server/model pair can honour it. Observed in production with LM Studio +
`openai/gpt-oss-20b` immediately after strict started reaching the wire:

    HTTP 400 from /v1/chat/completions — server said:
    {"error":"Engine protocol predict stream returned an error:
     {\\"code\\":500,\\"message\\":\\"The model produced output that does not
      match the expected peg-native format\\"}"}

Every review cycle failed. Before, the flag was silently dropped, the model
free-formed, and `_parse_decision` handled the slack — so making the flag work
took a system that traded and stopped it trading.

The underlying finding stands: dropping `strict` silently contradicted
`ai/schemas.py` and `ai/backends/base.py`, which both claim the local path vets
orders identically. The honest resolution is that the guarantee is only
available where the server can provide it — so it is a config decision, and it
defaults OFF for the OpenAI-compatible path to match the behaviour that was
actually shipping. The Anthropic backend is untouched; it has always sent
`strict` and honours it.
"""

from __future__ import annotations

from poseidon.ai.backends.openai_backend import _to_openai_tools
from poseidon.ai.schemas import SUBMIT_DECISION_TOOL
from poseidon.core.config import AIConfig


def test_default_is_off_so_a_working_local_setup_keeps_working() -> None:
    assert AIConfig().strict_tools is False


def test_strict_is_omitted_by_default() -> None:
    fn = _to_openai_tools([SUBMIT_DECISION_TOOL])[0]["function"]
    assert "strict" not in fn


def test_strict_is_sent_when_enabled() -> None:
    fn = _to_openai_tools([SUBMIT_DECISION_TOOL], strict=True)[0]["function"]
    assert fn["strict"] is True


def test_enabling_it_only_marks_tools_that_declare_it() -> None:
    """A loose read-only tool schema under grammar constraint would start
    rejecting valid calls."""
    loose = {"name": "get_quote", "description": "", "input_schema": {"type": "object"}}
    out = _to_openai_tools([loose, SUBMIT_DECISION_TOOL], strict=True)
    assert "strict" not in out[0]["function"]
    assert out[1]["function"]["strict"] is True


def test_schema_still_travels_either_way() -> None:
    for strict in (False, True):
        fn = _to_openai_tools([SUBMIT_DECISION_TOOL], strict=strict)[0]["function"]
        assert fn["parameters"] == SUBMIT_DECISION_TOOL["input_schema"]
        assert fn["parameters"].get("additionalProperties") is False
