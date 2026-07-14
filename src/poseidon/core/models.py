"""Domain models.

Every model that carries market data has an ``as_of`` timestamp (UTC) and a
``source`` identifying the provider that produced it. The freshness rules in
:mod:`poseidon.core.clock` reject stale data before it can reach the AI
or the execution path — the platform never trades on fabricated or aged data.

Monetary values use :class:`decimal.Decimal` end to end; floats are accepted
at the boundary and coerced, never used internally for money math.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class PoseidonModel(BaseModel):
    """Base model: mutable with validated assignment (``frozen=False``), strict-ish validation (``extra="forbid"``)."""

    model_config = ConfigDict(frozen=False, extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


class Quote(PoseidonModel):
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


class Bar(PoseidonModel):
    symbol: str
    open: Money
    high: Money
    low: Money
    close: Money
    volume: int
    start: datetime
    end: datetime
    source: str


class Greeks(PoseidonModel):
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    implied_volatility: float | None = None


class OptionContract(PoseidonModel):
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


class OptionChain(PoseidonModel):
    underlying: str
    expirations: list[date]
    contracts: list[OptionContract]
    as_of: datetime
    source: str


class NewsArticle(PoseidonModel):
    id: str = Field(default_factory=new_id)
    headline: str
    summary: str | None = None
    url: str | None = None
    symbols: list[str] = Field(default_factory=list)
    published_at: datetime
    source: str
    sentiment: float | None = None  # provider-supplied only; never inferred locally


class EarningsEvent(PoseidonModel):
    symbol: str
    report_date: date
    time_hint: str | None = None  # "bmo" (before open) / "amc" (after close) / None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None
    as_of: datetime
    source: str


class EconomicEvent(PoseidonModel):
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


class Position(PoseidonModel):
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


class TaxLot(PoseidonModel):
    symbol: str
    quantity: Decimal
    cost_basis: Money
    acquired_at: datetime
    broker: str = ""


class AccountSnapshot(PoseidonModel):
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


class Dividend(PoseidonModel):
    symbol: str
    amount: Money
    pay_date: date
    broker: str = ""


class Fill(PoseidonModel):
    order_id: str
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Money
    filled_at: datetime
    broker: str = ""


class Transfer(PoseidonModel):
    """External cash movement (deposit, withdrawal, or journal). ``amount``
    is signed: positive moves cash INTO the account. Not trading P&L — the
    sync service re-anchors the loss/drawdown baselines by the net flow."""

    id: str
    at: datetime
    amount: Money  # signed: +deposit / -withdrawal


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


class OptionLeg(PoseidonModel):
    contract_symbol: str
    side: OrderSide
    quantity: int  # contracts


class Order(PoseidonModel):
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
    strategy: str = ""  # originating strategy (performance attribution)
    # Execution quality (TCA): live mid at final risk validation is the
    # arrival price; slippage is signed so positive = cost to the account.
    arrival_price: Money | None = None
    slippage_bps: float | None = None
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
        """Best-effort notional for risk checks, sized conservatively at the
        highest known price (limit, stop trigger, or live reference) so a
        buy-stop entry is checked at its trigger price, not the lower current
        market. Requires a live reference price for market orders."""
        candidates = [p for p in (self.limit_price, self.stop_price, reference_price) if p is not None]
        if not candidates:
            return None
        return abs(self.quantity) * max(candidates)


# --------------------------------------------------------------------------
# AI decisions & explainability
# --------------------------------------------------------------------------


class ExitPlan(PoseidonModel):
    stop_loss: Money | None = None
    take_profit: Money | None = None
    time_stop: str | None = None  # e.g. "exit before earnings on 2026-07-24"
    notes: str | None = None


class TradeRationale(PoseidonModel):
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


class ProposedTrade(PoseidonModel):
    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    limit_price: Money | None = None
    stop_price: Money | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    legs: list[OptionLeg] = Field(default_factory=list)
    strategy: str = ""  # name of the strategy this trade belongs to
    # Per-trade exit levels for the position guardian. A decision may open
    # several positions; each carries its OWN stop/target so the guardian
    # never arms one symbol's stop against another's price.
    stop_loss: Money | None = None
    take_profit: Money | None = None

    @model_validator(mode="after")
    def _require_prices_for_type(self) -> ProposedTrade:
        # A limit / stop-limit trade needs a limit_price; a stop / stop-limit
        # needs a stop_price. Without this a price-less LIMIT proposal reaches
        # a broker that fills it AT MARKET, bypassing SlippageProtectionRule's
        # fat-finger guard. Rejected here voids the whole decision in
        # _parse_decision (coupled legs must not partially execute).
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} order requires a limit_price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError(f"{self.order_type.value} order requires a stop_price")
        return self


class Decision(PoseidonModel):
    """One decision emitted by the AI portfolio manager for a review cycle."""

    id: str = Field(default_factory=new_id)
    action: DecisionAction
    trades: list[ProposedTrade] = Field(default_factory=list)
    rationale: TradeRationale | None = None  # required whenever trades are proposed
    data_sources: list[str] = Field(default_factory=list)  # provenance of inputs used
    data_gaps: list[str] = Field(default_factory=list)  # data needed but unavailable this cycle
    summary: str = ""  # one-paragraph cycle summary for the log/dashboard
    model: str = ""
    cycle_id: str = ""
    usage: dict[str, int] = Field(default_factory=dict)  # tokens used this cycle
    created_at: datetime | None = None


# --------------------------------------------------------------------------
# Misc runtime records
# --------------------------------------------------------------------------


class ComponentHealth(PoseidonModel):
    name: str
    state: str
    detail: str | None = None
    latency_ms: float | None = None
    checked_at: datetime


class AuditRecord(PoseidonModel):
    """One append-only audit entry. ``prev_hash``/``hash`` form a tamper-evident
    chain (see security/audit.py)."""

    seq: int
    at: datetime
    actor: str  # "ai", "human", "system", broker name, ...
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str
