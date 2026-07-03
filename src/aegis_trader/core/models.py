"""Domain models.

Every model that carries market data has an ``as_of`` timestamp (UTC) and a
``source`` identifying the provider that produced it. The freshness rules in
:mod:`aegis_trader.core.clock` reject stale data before it can reach the AI
or the execution path — the platform never trades on fabricated or aged data.

Monetary values use :class:`decimal.Decimal` end to end; floats are accepted
at the boundary and coerced, never used internally for money math.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    AssetClass,
    DataFreshness,
    DecisionAction,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)

Money = Annotated[Decimal, Field(allow_inf_nan=False)]


def new_id() -> str:
    return uuid.uuid4().hex


class AegisModel(BaseModel):
    """Base model: immutable-by-default, strict-ish validation."""

    model_config = ConfigDict(frozen=False, extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


class Quote(AegisModel):
    symbol: str
    bid: Money | None = None
    ask: Money | None = None
    last: Money | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    volume: int | None = None
    as_of: datetime
    source: str
    freshness: DataFreshness = DataFreshness.REAL_TIME

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_pct(self) -> Decimal | None:
        """Bid/ask spread as a fraction of the mid price (liquidity filter input)."""
        mid = self.mid
        if self.bid is None or self.ask is None or mid is None or mid <= 0:
            return None
        return (self.ask - self.bid) / mid

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class Bar(AegisModel):
    symbol: str
    open: Money
    high: Money
    low: Money
    close: Money
    volume: int
    start: datetime
    end: datetime
    source: str


class Greeks(AegisModel):
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    implied_volatility: float | None = None


class OptionContract(AegisModel):
    symbol: str  # OCC symbol, e.g. AAPL240621C00190000
    underlying: str
    right: OptionRight
    strike: Money
    expiration: date
    bid: Money | None = None
    ask: Money | None = None
    last: Money | None = None
    volume: int | None = None
    open_interest: int | None = None
    greeks: Greeks | None = None
    as_of: datetime
    source: str


class OptionChain(AegisModel):
    underlying: str
    expirations: list[date]
    contracts: list[OptionContract]
    as_of: datetime
    source: str


class NewsArticle(AegisModel):
    id: str = Field(default_factory=new_id)
    headline: str
    summary: str | None = None
    url: str | None = None
    symbols: list[str] = Field(default_factory=list)
    published_at: datetime
    source: str
    sentiment: float | None = None  # provider-supplied only; never inferred locally


class EarningsEvent(AegisModel):
    symbol: str
    report_date: date
    time_hint: str | None = None  # "bmo" (before open) / "amc" (after close) / None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None
    as_of: datetime
    source: str


class EconomicEvent(AegisModel):
    name: str
    country: str
    scheduled_at: datetime
    importance: str | None = None  # provider scale, e.g. low/medium/high
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    as_of: datetime
    source: str


# --------------------------------------------------------------------------
# Portfolio / account
# --------------------------------------------------------------------------


class Position(AegisModel):
    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    quantity: Decimal
    avg_entry_price: Money
    market_value: Money | None = None
    unrealized_pnl: Money | None = None
    realized_pnl_today: Money | None = None
    greeks: Greeks | None = None  # populated for option positions
    broker: str = ""
    as_of: datetime


class TaxLot(AegisModel):
    symbol: str
    quantity: Decimal
    cost_basis: Money
    acquired_at: datetime
    broker: str = ""


class AccountSnapshot(AegisModel):
    broker: str
    account_id: str
    equity: Money
    cash: Money
    buying_power: Money
    maintenance_margin: Money | None = None
    margin_used: Money | None = None
    options_buying_power: Money | None = None
    day_pnl: Money | None = None
    as_of: datetime


class Dividend(AegisModel):
    symbol: str
    amount: Money
    pay_date: date
    broker: str = ""


class Fill(AegisModel):
    order_id: str
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Money
    filled_at: datetime
    broker: str = ""


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


class OptionLeg(AegisModel):
    contract_symbol: str
    side: OrderSide
    quantity: int  # contracts


class Order(AegisModel):
    """A single order tracked through its full lifecycle.

    ``client_order_id`` is generated once and passed to the broker for
    idempotency — resubmitting the same order object can never double-fill.
    """

    id: str = Field(default_factory=new_id)
    client_order_id: str = Field(default_factory=new_id)
    broker: str = ""
    broker_order_id: str | None = None
    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    quantity: Decimal
    limit_price: Money | None = None
    stop_price: Money | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    extended_hours: bool = False
    legs: list[OptionLeg] = Field(default_factory=list)  # multi-leg option orders
    status: OrderStatus = OrderStatus.PROPOSED
    status_reason: str | None = None
    filled_quantity: Decimal = Decimal(0)
    avg_fill_price: Money | None = None
    decision_id: str | None = None  # links back to the AI decision that produced it
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("quantity")
    @classmethod
    def _positive_qty(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("order quantity must be positive")
        return v

    def estimated_notional(self, reference_price: Decimal | None = None) -> Decimal | None:
        """Best-effort notional for risk checks; requires a live reference price
        for market orders."""
        price = self.limit_price or reference_price
        if price is None:
            return None
        return abs(self.quantity) * price


# --------------------------------------------------------------------------
# AI decisions & explainability
# --------------------------------------------------------------------------


class ExitPlan(AegisModel):
    stop_loss: Money | None = None
    take_profit: Money | None = None
    time_stop: str | None = None  # e.g. "exit before earnings on 2026-07-24"
    notes: str | None = None


class TradeRationale(AegisModel):
    """The mandatory explainability report attached to every decision."""

    thesis: str  # why enter
    timing: str  # why now
    expected_edge: str
    risk: str
    reward: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_indicators: list[str] = Field(default_factory=list)
    supporting_news: list[str] = Field(default_factory=list)
    portfolio_impact: str
    exit_plan: ExitPlan
    max_expected_loss: str
    alternative_scenarios: list[str] = Field(default_factory=list)


class ProposedTrade(AegisModel):
    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    quantity: Decimal
    limit_price: Money | None = None
    stop_price: Money | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    legs: list[OptionLeg] = Field(default_factory=list)
    strategy: str = ""  # name of the strategy this trade belongs to


class Decision(AegisModel):
    """One decision emitted by the AI portfolio manager for a review cycle."""

    id: str = Field(default_factory=new_id)
    action: DecisionAction
    trades: list[ProposedTrade] = Field(default_factory=list)
    rationale: TradeRationale | None = None  # required whenever trades are proposed
    data_sources: list[str] = Field(default_factory=list)  # provenance of inputs used
    model: str = ""
    cycle_id: str = ""
    created_at: datetime | None = None


# --------------------------------------------------------------------------
# Misc runtime records
# --------------------------------------------------------------------------


class ComponentHealth(AegisModel):
    name: str
    state: str
    detail: str | None = None
    latency_ms: float | None = None
    checked_at: datetime


class AuditRecord(AegisModel):
    """One append-only audit entry. ``prev_hash``/``hash`` form a tamper-evident
    chain (see security/audit.py)."""

    seq: int
    at: datetime
    actor: str  # "ai", "human", "system", broker name, ...
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str
