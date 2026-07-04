"""Claude algorithm review: adapt pasted code into a workshop algorithm.

The operator pastes an algorithm written for any platform (Pine Script,
thinkScript, QuantConnect/Python, pseudocode, ...) plus optional
instructions. Claude analyzes it, flags risks and assumptions, and — when
the logic is expressible as a screener — produces a ready-to-save
implementation of the Poseidon contract (``async def scan(ctx)``).

The produced source is machine-validated with the same static screen the
workshop applies on save; if it fails, the model gets exactly one retry
with the validator's errors. Nothing here activates anything: the result
is returned to the operator (or saved as a *draft*), and activation stays
a human decision.
"""

from __future__ import annotations

from typing import Any, cast

import anthropic
import structlog

from ..core.errors import AgentError
from ..strategy.custom import validate_algorithm

log = structlog.get_logger(__name__)

_REVIEW_TOOL: dict[str, Any] = {
    "name": "submit_algorithm_review",
    "description": "Submit the completed review. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "analysis": {"type": "string", "description": "What the algorithm does, in plain language"},
            "risks": {"type": "array", "items": {"type": "string"},
                      "description": "Weaknesses, hidden assumptions, overfitting/lookahead concerns"},
            "recommendations": {"type": "array", "items": {"type": "string"},
                                "description": "Concrete improvements, whether or not applied"},
            "convertible": {"type": "boolean",
                            "description": "Whether the logic can run as an Poseidon screener"},
            "poseidon_source": {"type": ["string", "null"],
                             "description": "Complete Poseidon implementation (async def scan(ctx)), or null if not convertible"},
            "suggested_name": {"type": "string"},
            "suggested_description": {"type": "string"},
            "conversion_notes": {"type": "string",
                                 "description": "What was changed/dropped relative to the original and why"},
        },
        "required": ["analysis", "risks", "recommendations", "convertible", "poseidon_source",
                     "suggested_name", "suggested_description", "conversion_notes"],
        "additionalProperties": False,
    },
}

_SYSTEM = """You are the algorithm reviewer for Poseidon, an autonomous \
trading platform. The operator pastes an algorithm written for another \
platform; you analyze it honestly and, when possible, convert it to Poseidon's \
workshop contract.

The contract — the source must define exactly this entry point:

    async def scan(ctx) -> list[dict]:

ctx provides ONLY:
  - await ctx.quote(symbol) -> Quote (bid/ask/last as Decimal, as_of, source)
  - await ctx.bars(symbol, timeframe="1d", limit=100) -> list[Bar] \
(open/high/low/close Decimal, volume int, start datetime)
  - await ctx.option_chain(symbol) -> OptionChain (contracts with greeks)
  - ctx.symbols (list[str]), ctx.params (dict), ctx.positions (list[dict]), \
ctx.equity (float), ctx.log(message)
Return rows: {"symbol": str, "direction": "long"|"short"|"exit"|"hedge"|"income", \
"strength": float 0..1, "evidence": dict}. Wrap per-symbol data access in \
try/except and continue — one symbol failing must not abort the scan.

Hard restrictions (statically enforced on save): no imports of os/sys/\
subprocess/socket/network/file modules; no open/exec/eval/__import__; no \
dunder attribute access; math/statistics/datetime/decimal/collections are \
fine. Algorithms are SCREENERS: they emit candidate signals for the AI \
portfolio manager to weigh — they cannot place orders, so do not write \
order/position-mutation logic; express exits as direction="exit" signals.

Review honestly: name lookahead bias, overfit parameters, repainting \
indicators, and platform features that do not translate (e.g. intrabar \
fills, tick data). If the core idea cannot work as a screener, say so and \
set convertible=false with poseidon_source=null rather than forcing it."""


async def review_algorithm(client: anthropic.AsyncAnthropic, model: str, *,
                           source: str, instructions: str = "",
                           max_tokens: int = 8000) -> dict[str, Any]:
    """One-shot review with a single validation retry. Returns the review
    dict plus ``validation_errors`` (empty when the produced source passes)
    and ``usage`` token counts for metering."""
    prompt = (
        "Review this algorithm and convert it to the Poseidon contract if possible.\n"
        + (f"Operator instructions: {instructions}\n" if instructions.strip() else "")
        + f"\n--- pasted algorithm ---\n{source[:40_000]}\n--- end ---"
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    usage = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}

    for attempt in (1, 2):
        try:
            response = await client.messages.create(
                model=model, max_tokens=max_tokens,
                system=_SYSTEM,
                tools=cast("Any", [_REVIEW_TOOL]),
                tool_choice=cast("Any", {"type": "tool", "name": "submit_algorithm_review"}),
                messages=cast("Any", messages),
            )
        except anthropic.APIError as exc:
            raise AgentError(f"algorithm review failed: {exc}") from exc
        usage["api_calls"] += 1
        block_usage = getattr(response, "usage", None)
        if block_usage is not None:
            usage["input_tokens"] += getattr(block_usage, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(block_usage, "output_tokens", 0) or 0
        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is None:
            raise AgentError("algorithm review returned no result")
        review = dict(tool_use.input)

        produced = review.get("poseidon_source")
        problems = validate_algorithm(str(produced)) if produced else []
        if not problems or attempt == 2:
            review["validation_errors"] = problems
            review["usage"] = usage
            if problems:
                log.warning("review source failed validation after retry", problems=problems)
            return review
        # One retry: hand the validator's output back.
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result", "tool_use_id": tool_use.id,
                "content": ("The produced poseidon_source failed static validation: "
                            + "; ".join(problems)
                            + ". Call submit_algorithm_review again with corrected source."),
                "is_error": True,
            }],
        })
    raise AgentError("unreachable")  # pragma: no cover
