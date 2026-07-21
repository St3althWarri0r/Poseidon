"""Post-trade reflection: distill a closed position into a short advisory lesson.

One completion through the ChatBackend seam — no tools, no dispatcher, no order
path (structurally like reviewer.py). Failure returns None; the caller skips
storage. The lesson is ADVISORY prose, never an audit fact and never a gate on
the risk engine.
"""
from __future__ import annotations

import structlog

from ..core.errors import AgentRefusedError
from ..core.models import ClosedPosition
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
        f"Realized return: {pos.realized_return * 100:+.2f}%. Alpha vs SPY: {alpha}.",
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
