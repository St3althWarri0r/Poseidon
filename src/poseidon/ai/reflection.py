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
from .backends.base import ChatBackend

log = structlog.get_logger(__name__)

REFLECTION_SYSTEM = """\
You review a trade that has already closed and write ONE short lesson for the \
portfolio manager's future decisions. Discipline:
- 2 to 4 sentences. Every word must earn its place.
- State whether the directional call was right, and cite the realized alpha.
- Say concisely what in the thesis worked or failed.
- End with exactly one actionable lesson for next time.
Write plain prose only — no preamble, no headings, no markdown, no numbers you \
were not given. This is retrospective: never assert a current market price."""


def _describe(pos: ClosedPosition) -> str:
    direction = "short" if pos.is_short else "long"
    alpha = "n/a" if pos.alpha is None else f"{pos.alpha * 100:+.2f}%"
    thesis = pos.thesis.strip() or "(no recorded thesis)"
    return (
        f"Closed {direction} {pos.symbol} (strategy: {pos.strategy or 'unattributed'}).\n"
        f"Entry {pos.entry_price} -> exit {pos.exit_price}, held {pos.holding_days:.1f} days.\n"
        f"Realized return: {pos.realized_return * 100:+.2f}%. Alpha vs SPY: {alpha}.\n"
        f"Original entry thesis: {thesis}\n\n"
        "Write the lesson now."
    )


async def reflect_on_position(backend: ChatBackend, pos: ClosedPosition, *,
                              model: str, max_chars: int = 600) -> str | None:
    messages = [{"role": "user", "content": _describe(pos)}]
    try:
        resp = await backend.complete(messages, tools=[], system=REFLECTION_SYSTEM)
    except AgentRefusedError:
        log.info("reflection refused", symbol=pos.symbol)
        return None
    except Exception as exc:  # best-effort: never propagate (covers AgentError)
        log.warning("reflection failed", symbol=pos.symbol, error=str(exc))
        return None
    text = (resp.text or "").strip()
    if not text:
        return None
    return text[:max_chars].strip()
