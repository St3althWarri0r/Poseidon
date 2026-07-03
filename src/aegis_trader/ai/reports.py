"""Explainability report rendering.

Every decision is persisted as structured JSON; this module renders the
human-facing Markdown used in notifications and the dashboard's reasoning
log.
"""

from __future__ import annotations

from ..core.models import Decision


def render_decision_report(decision: Decision) -> str:
    lines: list[str] = [
        f"# Decision {decision.id[:8]} — {decision.action.value.replace('_', ' ').title()}",
        "",
        f"*Cycle:* `{decision.cycle_id}` · *Model:* `{decision.model}` · "
        f"*At:* {decision.created_at.isoformat() if decision.created_at else 'n/a'}",
        f"*Live data sources used:* {', '.join(decision.data_sources) or 'none'}",
        "",
    ]
    if decision.trades:
        lines.append("## Proposed trades")
        for t in decision.trades:
            price = f" @ {t.limit_price}" if t.limit_price is not None else " (market)"
            lines.append(
                f"- **{t.side.value.replace('_', ' ')} {t.quantity} {t.symbol}**"
                f"{price} · {t.order_type.value} / {t.time_in_force.value}"
                f" · strategy: {t.strategy or 'n/a'}"
            )
        lines.append("")
    r = decision.rationale
    if r is not None:
        exit_bits = []
        if r.exit_plan.stop_loss is not None:
            exit_bits.append(f"stop loss {r.exit_plan.stop_loss}")
        if r.exit_plan.take_profit is not None:
            exit_bits.append(f"take profit {r.exit_plan.take_profit}")
        if r.exit_plan.time_stop:
            exit_bits.append(f"time stop: {r.exit_plan.time_stop}")
        lines += [
            "## Rationale",
            f"**Why enter:** {r.thesis}",
            f"**Why now:** {r.timing}",
            f"**Expected edge:** {r.expected_edge}",
            f"**Risk:** {r.risk}",
            f"**Reward:** {r.reward}",
            f"**Confidence:** {r.confidence:.0%}",
            f"**Portfolio impact:** {r.portfolio_impact}",
            f"**Exit plan:** {'; '.join(exit_bits) or 'see notes'}"
            + (f" — {r.exit_plan.notes}" if r.exit_plan.notes else ""),
            f"**Maximum expected loss:** {r.max_expected_loss}",
        ]
        if r.supporting_indicators:
            lines.append("**Supporting indicators:** " + "; ".join(r.supporting_indicators))
        if r.supporting_news:
            lines.append("**Supporting news:**")
            lines += [f"  - {n}" for n in r.supporting_news]
        if r.alternative_scenarios:
            lines.append("**Alternative scenarios:**")
            lines += [f"  - {s}" for s in r.alternative_scenarios]
    return "\n".join(lines)
