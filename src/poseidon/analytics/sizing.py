"""Volatility-targeted position sizing.

Equalizes risk across positions instead of equalizing notional: a quiet
mega-cap and a volatile small-cap sized by this method contribute the
same expected daily dollar move to the account. The suggestion is
advisory — every order still passes the full risk engine — but it gives
the AI a disciplined starting point instead of round numbers.

    target daily $ risk = equity × risk_budget_pct
    suggested shares    = target / (price × daily_vol)

capped by the position-size limit and live buying power.
"""

from __future__ import annotations

from typing import Any

TRADING_DAYS = 252


def daily_volatility(closes: list[float], window: int = 20) -> float | None:
    """Close-to-close daily return volatility (NOT annualized)."""
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0
            for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return float(var ** 0.5)


def suggest_size(*, equity: float, price: float, daily_vol: float,
                 risk_budget_pct: float, max_position_pct: float,
                 buying_power: float) -> dict[str, Any]:
    """Pure sizing computation. All inputs must come from live data."""
    if equity <= 0 or price <= 0:
        return {"error": "no usable equity/price"}
    target_dollar_risk = equity * risk_budget_pct
    if daily_vol <= 0:
        return {"error": "volatility is zero — cannot vol-target"}
    raw_shares = target_dollar_risk / (price * daily_vol)

    caps: list[str] = []
    max_by_position_limit = (equity * max_position_pct) / price
    if raw_shares > max_by_position_limit:
        caps.append(f"max_position_pct ({max_position_pct:.0%} of equity)")
    max_by_buying_power = max(buying_power, 0.0) / price
    if raw_shares > max_by_buying_power:
        caps.append("buying power")
    shares = int(min(raw_shares, max_by_position_limit, max_by_buying_power))

    return {
        "suggested_shares": shares,
        "uncapped_shares": round(raw_shares, 2),
        "capped_by": caps,
        "target_daily_dollar_risk": round(target_dollar_risk, 2),
        "estimated_daily_dollar_move": round(shares * price * daily_vol, 2),
        "notional": round(shares * price, 2),
        "notional_pct_of_equity": round(shares * price / equity, 4),
        "inputs": {
            "price": round(price, 4),
            "daily_volatility": round(daily_vol, 5),
            "annualized_volatility": round(daily_vol * TRADING_DAYS ** 0.5, 4),
            "risk_budget_pct": risk_budget_pct,
        },
        "note": (
            "Advisory vol-targeted size; every order still passes the full risk "
            "engine. A suggestion of 0 means the risk budget cannot buy one share."
        ),
    }
