"""`strict` must survive translation to the OpenAI tool shape.

`submit_decision` is declared `strict: True` with `additionalProperties: false`
and a complete `required` list (`ai/schemas.py`). On the Anthropic path that is
what makes a malformed decision structurally impossible. `_to_openai_tools`
copied only name/description/parameters, so on the `openai_compatible` backend —
the one the live config actually uses — the flag never reached the wire and the
server applied no grammar-constrained decoding. `required`, `enum` and
`additionalProperties` were decorative.

That mattered because two docstrings promise otherwise: `ai/schemas.py` ("a
malformed decision cannot slip through") and `ai/backends/base.py` ("swapping
Anthropic for a local OpenAI-compatible model changes nothing about how orders
are vetted").

The pre-existing test asserted `SUBMIT_DECISION_TOOL["strict"] is True` — it
pinned the flag *in the dict* and never checked it was transmitted, so the gap
was invisible to CI. These tests pin the transmission.
"""

from __future__ import annotations

from poseidon.ai.backends.openai_backend import _to_openai_tools
from poseidon.ai.schemas import ALL_TOOLS, SUBMIT_DECISION_TOOL


def test_strict_reaches_the_wire_for_submit_decision() -> None:
    fn = _to_openai_tools([SUBMIT_DECISION_TOOL])[0]["function"]
    assert fn.get("strict") is True, (
        "submit_decision declares strict: True; dropping it in translation means "
        "the local model gets no schema enforcement on the decision payload"
    )


def test_strict_is_only_set_where_the_source_tool_declares_it() -> None:
    """Read-only data tools are not strict, and must not silently become so —
    strict + additionalProperties:false on a loose schema would reject valid
    calls rather than accept invalid ones."""
    for src, out in zip(ALL_TOOLS, _to_openai_tools(ALL_TOOLS), strict=True):
        assert out["function"].get("strict") == src.get("strict"), src["name"]


def test_schema_and_name_still_translate() -> None:
    fn = _to_openai_tools([SUBMIT_DECISION_TOOL])[0]["function"]
    assert fn["name"] == SUBMIT_DECISION_TOOL["name"]
    assert fn["parameters"] == SUBMIT_DECISION_TOOL["input_schema"]
    assert fn["parameters"].get("additionalProperties") is False
