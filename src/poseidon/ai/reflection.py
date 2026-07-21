"""Post-trade reflection: distill a closed position into a short advisory lesson.

One completion through the ChatBackend seam — no tools, no dispatcher, no order
path (structurally like reviewer.py). Failure returns None; the caller skips
storage. The lesson is ADVISORY prose, never an audit fact and never a gate on
the risk engine.

:func:`reflect_on_outcome` is the counterfactual sibling: it grades a decision
that never became a trade (or a deliberate hold) at a fixed forward horizon,
with the same failure and single-line-collapse discipline.
"""
from __future__ import annotations

import structlog

from ..core.errors import AgentRefusedError
from ..core.models import ClosedPosition, DecisionOutcome
from .backends import add_usage
from .backends.base import ChatBackend

log = structlog.get_logger(__name__)

REFLECTION_SYSTEM = """\
You review a trade that has already closed and write ONE short lesson for the \
portfolio manager's future decisions. Discipline:
- 2 to 4 sentences. Every word must earn its place.
- State whether the directional call was right, and cite the realized alpha.
- Say concisely what in the thesis worked or failed.
- When entry conviction is given, judge whether the outcome earned it — name \
overconfidence and underconfidence plainly, and note if the stated invalidation \
was the right tripwire.
- End with exactly one actionable lesson for next time.
Write plain prose only — no preamble, no headings, no markdown, no numbers you \
were not given. This is retrospective: never assert a current market price."""


def _describe(pos: ClosedPosition) -> str:
    direction = "short" if pos.is_short else "long"
    alpha = "n/a" if pos.alpha is None else f"{pos.alpha * 100:+.2f}%"
    thesis = pos.thesis.strip() or "(no recorded thesis)"
    lines = [
        f"Closed {direction} {pos.symbol} (strategy: {pos.strategy or 'unattributed'}).",
        f"Entry {pos.entry_price} -> exit {pos.exit_price}, held {pos.holding_days:.1f} days.",
        f"Realized return: {pos.realized_return * 100:+.2f}%. Alpha vs {pos.benchmark}: {alpha}.",
        f"Original entry thesis: {thesis}",
    ]
    # Only when recorded at entry — legacy episodes must not grow noise lines.
    if pos.entry_confidence is not None:
        lines.append(f"Entry conviction: {pos.entry_confidence:.0%}.")
    invalidation = pos.invalidation.strip()
    if invalidation:
        lines.append(f"Stated invalidation: {invalidation}")
    lines += ["", "Write the lesson now."]
    return "\n".join(lines)


async def reflect_on_position(backend: ChatBackend, pos: ClosedPosition, *,
                              model: str, max_chars: int = 600,
                              usage: list[dict[str, int]] | None = None) -> str | None:
    messages = [{"role": "user", "content": _describe(pos)}]
    try:
        resp = await backend.complete(messages, tools=[], system=REFLECTION_SYSTEM)
    except AgentRefusedError as exc:
        add_usage(usage, getattr(exc, "usage", None))  # refusals still bill
        log.info("reflection refused", symbol=pos.symbol)
        return None
    except Exception as exc:  # best-effort: never propagate (covers AgentError)
        add_usage(usage, getattr(exc, "usage", None))
        log.warning("reflection failed", symbol=pos.symbol, error=str(exc))
        return None
    add_usage(usage, getattr(resp, "usage", None))
    text = (resp.text or "").strip()
    if not text:
        return None
    # Collapse to a single printable line: internal newlines/tabs/control chars
    # would otherwise let a lesson break out of its advisory bullet when the
    # prompt is assembled, weakening the "not instructions" framing.
    cleaned = "".join(c for c in " ".join(text.split()) if c.isprintable())
    return cleaned[:max_chars].strip() or None


COUNTERFACTUAL_SYSTEM = """\
You review a trading decision that did NOT become a trade — or a deliberate \
hold — now graded at a fixed forward horizon, and write ONE short calibration \
lesson for the portfolio manager. Discipline:
- This is ONE observation, not a pattern: weigh it against base rates, and say \
plainly when the sample is too small to conclude anything.
- A missed rally is NOT evidence to chase, and a dodged loss is NOT evidence \
the process is safe: judge whether the decision process was sound given what \
was knowable at the time.
- NEVER suggest bypassing or loosening risk limits.
- 2 to 3 sentences, ending with exactly one calibration lesson.
Write plain prose only — no preamble, no headings, no markdown, no numbers you \
were not given. This is retrospective: never assert a current market price."""

_BLOCKED_PHRASES = {
    "rejected_risk": "The proposal was vetoed by the risk engine.",
    "rejected_human": "The proposal was declined by the human operator.",
    "unfilled": "The order was submitted but never filled.",
}


def _describe_outcome(o: DecisionOutcome) -> str:
    bench = "n/a" if o.benchmark_return is None else f"{o.benchmark_return * 100:+.2f}%"
    alpha = "n/a" if o.alpha is None else f"{o.alpha * 100:+.2f}%"
    when = o.decided_at.date().isoformat()
    if o.kind == "hold":
        lines = [
            f"Decision under review: {o.action} on {when} — no trades were proposed.",
            f"Portfolio forward return over the next {o.horizon_trading_days} trading "
            f"days: {o.forward_return * 100:+.2f}%.",
            f"Benchmark ({o.benchmark}) over the same window: {bench}. Alpha: {alpha}.",
        ]
    else:
        lines = [
            f"Decision under review: proposed {o.side or o.action} {o.symbol} on {when}.",
            "This decision did NOT become a trade.",
        ]
        blocked = _BLOCKED_PHRASES.get(o.blocked_status)
        if blocked:
            lines.append(blocked)
        # Only when recorded — legacy decisions must not grow noise lines
        # (same discipline as _describe above).
        thesis = o.thesis.strip()
        if thesis:
            lines.append(f"Original thesis: {thesis}")
        if o.entry_confidence is not None:
            lines.append(f"Entry conviction: {o.entry_confidence:.0%}.")
        invalidation = o.invalidation.strip()
        if invalidation:
            lines.append(f"Stated invalidation: {invalidation}")
        lines.append(
            f"Hypothetical forward return over the next {o.horizon_trading_days} trading "
            f"days, in the proposed direction: {o.forward_return * 100:+.2f}%.")
        lines.append(f"Benchmark ({o.benchmark}) over the same window: {bench}. Alpha: {alpha}.")
    lines += ["", "Write the lesson now."]
    return "\n".join(lines)


async def reflect_on_outcome(backend: ChatBackend, outcome: DecisionOutcome, *,
                             model: str, max_chars: int = 600,
                             usage: list[dict[str, int]] | None = None) -> str | None:
    messages = [{"role": "user", "content": _describe_outcome(outcome)}]
    try:
        resp = await backend.complete(messages, tools=[], system=COUNTERFACTUAL_SYSTEM)
    except AgentRefusedError as exc:
        add_usage(usage, getattr(exc, "usage", None))  # refusals still bill
        log.info("outcome reflection refused", symbol=outcome.symbol)
        return None
    except Exception as exc:  # best-effort: never propagate (covers AgentError)
        add_usage(usage, getattr(exc, "usage", None))
        log.warning("outcome reflection failed", symbol=outcome.symbol, error=str(exc))
        return None
    add_usage(usage, getattr(resp, "usage", None))
    text = (resp.text or "").strip()
    if not text:
        return None
    # Same single-printable-line collapse as reflect_on_position: outcome prose
    # must not break out of its advisory bullet either.
    cleaned = "".join(c for c in " ".join(text.split()) if c.isprintable())
    return cleaned[:max_chars].strip() or None
