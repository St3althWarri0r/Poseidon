"""Fundamentals digest for the fundamentals analyst's desk context.

``render_fundamentals_digest`` is pure and deterministic: it pins the exact
reported numbers (str(Decimal) verbatim — the snapshot exactness rule) into a
single bounded text block, and deliberately EXCLUDES the free-prose company
description — numbers and taxonomy only, minimizing the injection surface that
flows into an analyst prompt. Absent figures are simply omitted, never
estimated.

``fundamentals_context`` is the best-effort retrieval seam AnalysisService
calls per analyzed symbol: it returns '' unless ``ai.fundamentals.enabled``
AND ``analyst_context`` are on, and '' on ANY failure — a fundamentals outage
must never sink the analysis pipeline.
"""
from __future__ import annotations

import structlog

from ...core.config import FundamentalsConfig
from ...core.models import FundamentalsOverview, FundamentalsReport, StatementPeriod
from ...data.router import DataRouter

log = structlog.get_logger(__name__)

_DIGEST_PERIODS = 3  # newest statement periods rendered

# Fixed render order for the canonical items (uncurated extras follow sorted),
# so the digest is byte-deterministic for a given report.
_ITEM_ORDER = (
    "revenue", "net_income", "gross_profit", "operating_income", "diluted_eps",
    "total_assets", "total_liabilities", "shareholder_equity",
    "cash_and_equivalents", "long_term_debt", "operating_cashflow",
    "capital_expenditures", "shares_outstanding",
)


def _overview_line(overview: FundamentalsOverview) -> str:
    parts: list[str] = []
    for label, text in (("name", overview.name), ("sector", overview.sector),
                        ("industry", overview.industry)):
        if text:
            parts.append(f"{label} {text}")
    for label, exact in (("market_cap", overview.market_cap),
                         ("revenue_ttm", overview.revenue_ttm),
                         ("eps_ttm", overview.eps_ttm),
                         ("analyst_target", overview.analyst_target),
                         ("shares_outstanding", overview.shares_outstanding)):
        if exact is not None:
            parts.append(f"{label} {exact}")  # str(Decimal) verbatim
    for label, ratio in (("pe", overview.pe_ratio), ("forward_pe", overview.forward_pe),
                         ("peg", overview.peg_ratio),
                         ("price_to_book", overview.price_to_book),
                         ("ev_to_ebitda", overview.ev_to_ebitda),
                         ("profit_margin", overview.profit_margin),
                         ("operating_margin", overview.operating_margin),
                         ("roe", overview.return_on_equity),
                         ("dividend_yield", overview.dividend_yield),
                         ("beta", overview.beta)):
        if ratio is not None:
            parts.append(f"{label} {ratio:.4f}")
    # NOTE: overview.description (free prose) is deliberately NOT rendered.
    return "; ".join(parts)


def _period_line(period: StatementPeriod) -> str:
    head = period.form or period.period
    end_label = "FY end" if period.period == "annual" else "Q end"
    filed = f" filed {period.filed.isoformat()}" if period.filed is not None else ""
    ordered = [k for k in _ITEM_ORDER if k in period.items]
    ordered += sorted(k for k in period.items if k not in _ITEM_ORDER)
    body = ", ".join(f"{k} {period.items[k]}" for k in ordered)
    return f"{head} {end_label} {period.fiscal_date_ending.isoformat()}{filed}: {body}"


def render_fundamentals_digest(report: FundamentalsReport, *, max_chars: int) -> str:
    """Single-block digest pinning exact filed numbers. Hard-capped to
    ``max_chars``, always ending on a whole line (never a mid-value cut)."""
    lines = [f"FUNDAMENTALS (filed/reported data; source {report.source}, "
             f"as_of {report.as_of.isoformat()}):"]
    if report.overview is not None:
        overview_line = _overview_line(report.overview)
        if overview_line:
            lines.append(overview_line)
    newest = sorted(report.statements, key=lambda p: p.fiscal_date_ending, reverse=True)
    lines.extend(_period_line(p) for p in newest[:_DIGEST_PERIODS])
    out: list[str] = []
    total = 0
    for line in lines:
        extra = len(line) + (1 if out else 0)  # +1 for the joining newline
        if total + extra > max_chars:
            break
        out.append(line)
        total += extra
    return "\n".join(out)


async def fundamentals_context(router: DataRouter, symbol: str,
                               config: FundamentalsConfig) -> str:
    """Best-effort desk context for the fundamentals analyst (the seam
    AnalysisService fills into run_analysts' role_contexts)."""
    if not (config.enabled and config.analyst_context):
        return ""
    try:
        report = await router.fundamentals(symbol)
    except Exception as exc:  # best-effort — never sink the analysis pipeline
        log.warning("fundamentals context unavailable", symbol=symbol, error=str(exc))
        return ""
    return render_fundamentals_digest(report, max_chars=config.digest_max_chars)
