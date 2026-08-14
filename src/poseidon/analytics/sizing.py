"""Volatility-targeted position sizing.

Equalizes risk across positions instead of equalizing notional: a quiet
mega-cap and a volatile small-cap sized by this method contribute the
same expected daily dollar move to the account. The suggestion is
advisory — every order still passes the full risk engine — but it gives
the AI a disciplined starting point instead of round numbers.

    target daily $ risk = equity × risk_budget_pct
    suggested shares    = target / (price × daily_vol)

capped by the position-size limit, live buying power, and the broker's own
per-order notional cap.
"""

from __future__ import annotations

import math
from typing import Any

TRADING_DAYS = 252

# Sub-unit precision for fractional assets (crypto). Eight decimals is one
# satoshi at BTC scale and finer than any broker's minimum increment.
_FRACTIONAL_DP = 8


def _floor_quantity(raw: float, *, fractional: bool) -> float | int:
    """Floor to a placeable quantity — never round up.

    Rounding up could breach the very cap that was just applied, so every
    reduction here is downward. Whole units for equities; ``_FRACTIONAL_DP``
    decimals for crypto, where truncating to an integer would floor any
    sub-unit size to zero and make the asset untradeable on a small account.
    """
    if raw <= 0:
        return 0.0 if fractional else 0
    if not fractional:
        return int(raw)
    scale: int = 10**_FRACTIONAL_DP
    floored: int = math.floor(raw * scale)
    return floored / scale


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
                 buying_power: float, max_order_notional: float | None = None,
                 fractional: bool = False) -> dict[str, Any]:
    """Pure sizing computation. All inputs must come from live data.

    ``max_order_notional`` is the BROKER's per-order cap for this asset class
    (Alpaca's $200k crypto limit, say), or None when the broker declares none.
    Without it a large account sizes by ``max_position_pct`` alone and proposes
    orders the broker simply refuses — 20% of a $42M account is $8.4M, 42x over
    the cap — and the model then declines to trade rather than sizing down, so
    nothing trades at all. A position larger than the cap is built across
    several capped orders, which is what the cycle prompt already instructs.

    ``fractional`` permits a sub-unit quantity. Whole-unit truncation is correct
    for equities and wrong for crypto: a $100 account sizing BTC floors to 0,
    making every asset priced above the account balance untradeable.
    """
    if equity <= 0 or price <= 0:
        return {"error": "no usable equity/price"}
    target_dollar_risk = equity * risk_budget_pct
    if daily_vol <= 0:
        return {"error": "volatility is zero — cannot vol-target"}
    raw_shares = target_dollar_risk / (price * daily_vol)

    caps: list[str] = []
    limits = [raw_shares]
    max_by_position_limit = (equity * max_position_pct) / price
    limits.append(max_by_position_limit)
    if raw_shares > max_by_position_limit:
        caps.append(f"max_position_pct ({max_position_pct:.0%} of equity)")
    max_by_buying_power = max(buying_power, 0.0) / price
    limits.append(max_by_buying_power)
    if raw_shares > max_by_buying_power:
        caps.append("buying power")
    if max_order_notional is not None and max_order_notional > 0:
        max_by_broker = max_order_notional / price
        limits.append(max_by_broker)
        if raw_shares > max_by_broker:
            caps.append(f"broker per-order cap ({max_order_notional:,.0f})")
    shares = _floor_quantity(min(limits), fractional=fractional)

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
