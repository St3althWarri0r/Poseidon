"""Tool schemas for the AI portfolio manager.

``submit_decision`` uses strict validation (``strict: true`` +
``additionalProperties: false``) so the model's decision payload always
parses into the Decision model — a malformed decision cannot slip through
and the model gets a validation retry instead of the platform guessing.
"""

from __future__ import annotations

from typing import Any

_SIDE_ENUM = ["buy", "sell", "buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"]
_ORDER_TYPE_ENUM = ["market", "limit", "stop", "stop_limit"]
_ACTION_ENUM = [
    "buy", "sell", "hedge", "hold", "rebalance",
    "reduce_exposure", "increase_exposure", "no_action",
]

RATIONALE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string", "description": "Why enter/exit this position"},
        "timing": {"type": "string", "description": "Why act now rather than later"},
        "expected_edge": {"type": "string"},
        "risk": {"type": "string", "description": "What can go wrong and how badly"},
        "reward": {"type": "string", "description": "The upside case"},
        "confidence": {"type": "number", "description": "0.0-1.0"},
        "supporting_indicators": {"type": "array", "items": {"type": "string"}},
        "supporting_news": {
            "type": "array", "items": {"type": "string"},
            "description": "Headlines/URLs retrieved this cycle that support the thesis",
        },
        "portfolio_impact": {"type": "string"},
        "exit_plan": {
            "type": "object",
            "properties": {
                "stop_loss": {"type": ["string", "null"], "description": "Price as decimal string"},
                "take_profit": {"type": ["string", "null"]},
                "time_stop": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["stop_loss", "take_profit", "time_stop", "notes"],
            "additionalProperties": False,
        },
        "max_expected_loss": {"type": "string"},
        "alternative_scenarios": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "thesis", "timing", "expected_edge", "risk", "reward", "confidence",
        "supporting_indicators", "supporting_news", "portfolio_impact",
        "exit_plan", "max_expected_loss", "alternative_scenarios",
    ],
    "additionalProperties": False,
}

TRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "Ticker or OCC option symbol"},
        "asset_class": {"type": "string", "enum": ["equity", "etf", "option"]},
        "side": {"type": "string", "enum": _SIDE_ENUM},
        "order_type": {"type": "string", "enum": _ORDER_TYPE_ENUM},
        "quantity": {"type": "string", "description": "Decimal string; contracts for options"},
        "limit_price": {"type": ["string", "null"], "description": "Required unless market order"},
        "stop_price": {"type": ["string", "null"]},
        "time_in_force": {"type": "string", "enum": ["day", "gtc"]},
        "strategy": {"type": "string", "description": "Which enabled strategy this belongs to"},
    },
    "required": ["symbol", "asset_class", "side", "order_type", "quantity",
                 "limit_price", "stop_price", "time_in_force", "strategy"],
    "additionalProperties": False,
}

SUBMIT_DECISION_TOOL: dict[str, Any] = {
    "name": "submit_decision",
    "description": (
        "Submit your final decision for this review cycle. Call exactly once, after "
        "you have gathered all the live data you need. If proposing trades, every "
        "price you cite must come from a tool result in this conversation. If required "
        "data was unavailable, choose action 'no_action' and explain in data_gaps."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _ACTION_ENUM},
            "trades": {"type": "array", "items": TRADE_SCHEMA},
            "rationale": {
                "anyOf": [RATIONALE_SCHEMA, {"type": "null"}],
                "description": "Required when trades is non-empty",
            },
            "data_gaps": {
                "type": "array", "items": {"type": "string"},
                "description": "Data you needed but could not obtain live this cycle",
            },
            "summary": {"type": "string", "description": "One-paragraph cycle summary for the log"},
        },
        "required": ["action", "trades", "rationale", "data_gaps", "summary"],
        "additionalProperties": False,
    },
}


def _simple_tool(name: str, description: str, properties: dict[str, Any],
                 required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


DATA_TOOLS: list[dict[str, Any]] = [
    _simple_tool(
        "get_quote",
        "Live quote (bid/ask/last, timestamped) for a stock or ETF. The only valid "
        "source for current prices.",
        {"symbol": {"type": "string"}}, ["symbol"],
    ),
    _simple_tool(
        "get_bars",
        "Historical OHLCV bars for trend, momentum, volatility, and unusual-volume "
        "analysis. timeframe: 1m, 5m, 15m, 1h, 1d, 1w.",
        {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": ["1m", "5m", "15m", "1h", "1d", "1w"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        ["symbol", "timeframe", "limit"],
    ),
    _simple_tool(
        "get_option_chain",
        "Live option chain with greeks and open interest. expiration optional "
        "(YYYY-MM-DD); omit for the nearest expiration(s).",
        {
            "underlying": {"type": "string"},
            "expiration": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
        },
        ["underlying", "expiration"],
    ),
    _simple_tool(
        "get_news",
        "Latest news articles from live feeds. Pass symbols for company news, "
        "empty for market-wide news.",
        {
            "symbols": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        ["symbols", "limit"],
    ),
    _simple_tool(
        "get_earnings_calendar",
        "Upcoming earnings dates with estimates from the live calendar.",
        {
            "days_ahead": {"type": "integer", "minimum": 1, "maximum": 30},
            "symbols": {"type": "array", "items": {"type": "string"}},
        },
        ["days_ahead", "symbols"],
    ),
    _simple_tool(
        "get_economic_calendar",
        "Upcoming economic releases (CPI, FOMC, payrolls, ...) from the live calendar.",
        {"days_ahead": {"type": "integer", "minimum": 1, "maximum": 14}}, ["days_ahead"],
    ),
    _simple_tool(
        "get_portfolio",
        "Current account snapshot: equity, cash, buying power, margin, positions "
        "with P&L, tax lots, recent fills, and open orders.",
        {}, [],
    ),
    _simple_tool(
        "get_risk_status",
        "Current risk-engine status: loss limits used, drawdown, circuit breaker, "
        "orders remaining today, and the hard limits your trades must fit inside.",
        {}, [],
    ),
    _simple_tool(
        "get_risk_metrics",
        "Portfolio risk metrics computed from live bar history: 1-day historical "
        "VaR and expected shortfall (95/99%), portfolio beta to the benchmark, "
        "annualized volatility, and the most correlated pair of holdings. Use this "
        "to judge whether adding a position concentrates risk the individual "
        "position limits cannot see.",
        {}, [],
    ),
]

ALL_TOOLS: list[dict[str, Any]] = [*DATA_TOOLS, SUBMIT_DECISION_TOOL]
