"""Attribution rollups for backtest reports.

Two entry points, one per engine mode:

  * :func:`attribute_trades` — round-trip trades from the entry/exit
    engine (``BacktestEngine``): winners/losers, exit-reason and symbol
    rollups, holding-period buckets.
  * :func:`attribute_contributions` — per-symbol P&L contributions from
    the rebalance backtester (no discrete exits there, so no exit-reason
    rollup; contributions already net per-order trading costs).

Deterministic output: every ranking sorts with an explicit symbol
tie-break; money rounds to 2 decimals, returns to 4. Honesty gate: fewer
than 3 closed trades yields an ``insufficient_trades`` marker with NO
statistical fields — a rollup over 2 trades is noise dressed as insight.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import TradeRecord


def _bucket(days: int) -> str:
    if days <= 5:
        return "0-5"
    if days <= 20:
        return "6-20"
    if days <= 60:
        return "21-60"
    return "60+"


def _bucket_counts(spans: Iterable[int]) -> dict[str, int]:
    counts = {"0-5": 0, "6-20": 0, "21-60": 0, "60+": 0}
    for days in spans:
        counts[_bucket(days)] += 1
    return counts


def attribute_trades(trades: list[TradeRecord], starting_cash: float) -> dict[str, Any]:
    """Rollups over CLOSED trades (``exit_price`` set); open trades are
    excluded — an unrealized position has no attributable outcome yet."""
    closed = [t for t in trades if t.exit_price is not None]
    if len(closed) < 3:
        return {"insufficient_trades": len(closed), "note_code": "need>=3_closed_trades"}

    winners = sorted((t for t in closed if t.pnl > 0),
                     key=lambda t: (-t.pnl, t.symbol, str(t.entry_date)))
    losers = sorted((t for t in closed if t.pnl < 0),
                    key=lambda t: (t.pnl, t.symbol, str(t.entry_date)))

    def row(t: TradeRecord) -> dict[str, Any]:
        return {"symbol": t.symbol, "entry_date": str(t.entry_date),
                "exit_date": str(t.exit_date), "pnl": round(t.pnl, 2),
                "reason": t.reason}

    by_reason: dict[str, list[TradeRecord]] = {}
    by_sym: dict[str, list[TradeRecord]] = {}
    for t in closed:
        by_reason.setdefault(t.reason, []).append(t)
        by_sym.setdefault(t.symbol, []).append(t)
    ranked = sorted(by_sym.items(),
                    key=lambda kv: (-abs(sum(t.pnl for t in kv[1])), kv[0]))
    by_symbol: dict[str, Any] = {
        sym: {"trades": len(ts), "total_pnl": round(sum(t.pnl for t in ts), 2),
              "win_rate": round(sum(1 for t in ts if t.pnl > 0) / len(ts), 3)}
        for sym, ts in ranked[:20]
    }
    if len(ranked) > 20:
        rest = ranked[20:]
        by_symbol["others"] = {
            "count": len(rest),
            "total_pnl": round(sum(t.pnl for _, ts in rest for t in ts), 2),
        }

    top5 = winners[:5]
    total_pnl = sum(t.pnl for t in closed)
    ex_top5 = ((total_pnl - sum(t.pnl for t in top5)) / starting_cash
               if starting_cash > 0 else 0.0)
    return {
        "winners_top5": [row(t) for t in top5],
        "losers_top5": [row(t) for t in losers[:5]],
        "return_ex_top5_winners": round(ex_top5, 4),
        "by_exit_reason": {
            reason: {"trades": len(ts),
                     "win_rate": round(sum(1 for t in ts if t.pnl > 0) / len(ts), 3),
                     "total_pnl": round(sum(t.pnl for t in ts), 2)}
            for reason, ts in sorted(by_reason.items())
        },
        "by_symbol": by_symbol,
        "holding_day_buckets": _bucket_counts(
            (t.exit_date - t.entry_date).days for t in closed if t.exit_date is not None
        ),
    }


def attribute_contributions(contrib: dict[str, float], days_held: dict[str, int],
                            trading_costs: float, starting_cash: float) -> dict[str, Any]:
    """Rollups over rebalance-mode per-symbol contributions (mark-to-mark
    P&L net of per-order costs). No exit-reason rollup — rotation books
    have no discrete exits."""
    winners = sorted(((s, v) for s, v in contrib.items() if v > 0),
                     key=lambda kv: (-kv[1], kv[0]))
    losers = sorted(((s, v) for s, v in contrib.items() if v < 0),
                    key=lambda kv: (kv[1], kv[0]))

    def row(sym: str, value: float) -> dict[str, Any]:
        return {"symbol": sym, "contribution": round(value, 2),
                "days_held": days_held.get(sym, 0)}

    ranked = sorted(contrib.items(), key=lambda kv: (-abs(kv[1]), kv[0]))
    by_symbol: dict[str, Any] = {
        sym: {"contribution": round(v, 2), "days_held": days_held.get(sym, 0)}
        for sym, v in ranked[:20]
    }
    if len(ranked) > 20:
        rest = ranked[20:]
        by_symbol["others"] = {"count": len(rest),
                               "contribution": round(sum(v for _, v in rest), 2)}

    top5_sum = sum(v for _, v in winners[:5])
    total = sum(contrib.values())
    ex_top5 = (total - top5_sum) / starting_cash if starting_cash > 0 else 0.0
    return {
        "winners_top5": [row(s, v) for s, v in winners[:5]],
        "losers_top5": [row(s, v) for s, v in losers[:5]],
        "return_ex_top5_winners": round(ex_top5, 4),
        "by_symbol": by_symbol,
        "holding_day_buckets": _bucket_counts(days_held.values()),
        "trading_costs": round(trading_costs, 2),
    }
