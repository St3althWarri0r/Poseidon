"""Enumerations shared across the platform.

All enums are string-valued so they serialize cleanly to JSON, SQLite, and
the dashboard websocket without adapter code.
"""

from __future__ import annotations

from enum import StrEnum


class TradingMode(StrEnum):
    """Human-approval operating modes (see docs/user-guide.md)."""

    RESEARCH = "research"  # Mode 1: analysis only, no orders ever leave the app
    APPROVAL = "approval"  # Mode 2: Claude proposes, the human approves each order
    AUTONOMOUS = "autonomous"  # Mode 3: Claude may execute within risk limits


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    OPTION = "option"
    CRYPTO = "crypto"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"
    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"

    @property
    def is_buy(self) -> bool:
        return self in (OrderSide.BUY, OrderSide.BUY_TO_OPEN, OrderSide.BUY_TO_CLOSE)


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    OPG = "opg"  # at the open
    CLS = "cls"  # at the close


class OrderStatus(StrEnum):
    # Internal pre-broker states
    PROPOSED = "proposed"  # produced by the AI, not yet risk-checked
    PENDING_APPROVAL = "pending_approval"  # awaiting human approval (Mode 2)
    APPROVED = "approved"  # cleared for submission
    REJECTED_RISK = "rejected_risk"  # blocked by the risk engine
    REJECTED_HUMAN = "rejected_human"  # declined by the human
    # Broker lifecycle states
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED_BROKER = "rejected_broker"
    EXPIRED = "expired"
    ERROR = "error"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED_RISK,
            OrderStatus.REJECTED_HUMAN,
            OrderStatus.REJECTED_BROKER,
            OrderStatus.EXPIRED,
            OrderStatus.ERROR,
        )

    @property
    def is_open_at_broker(self) -> bool:
        return self in (
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        )


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


class DecisionAction(StrEnum):
    """Actions the AI portfolio manager may take."""

    BUY = "buy"
    SELL = "sell"
    HEDGE = "hedge"
    HOLD = "hold"
    REBALANCE = "rebalance"
    REDUCE_EXPOSURE = "reduce_exposure"
    INCREASE_EXPOSURE = "increase_exposure"
    NO_ACTION = "no_action"


class MarketSession(StrEnum):
    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"


class DataFreshness(StrEnum):
    REAL_TIME = "real_time"
    DELAYED = "delayed"
    STALE = "stale"  # older than the configured staleness threshold; unusable


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class NotificationLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class BrokerCapability(StrEnum):
    """Feature flags a broker plugin declares so the platform never calls
    an endpoint the broker cannot serve."""

    EQUITIES = "equities"
    OPTIONS = "options"
    CRYPTO = "crypto"
    FRACTIONAL_SHARES = "fractional_shares"
    EXTENDED_HOURS = "extended_hours"
    PAPER_TRADING = "paper_trading"
    STREAMING = "streaming"
    TAX_LOTS = "tax_lots"
    MARGIN = "margin"
