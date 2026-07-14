"""Loss/drawdown baselines must not carry across a same-broker account switch.

account_scope is ``name:paper|live`` (no account_id), so connecting a DIFFERENT
real account at the SAME brokerage keeps the same scope string and the baseline
guard would otherwise restore the previous account's day/week loss + drawdown
anchors — mis-anchoring the safety halts. The sync service must detect the
account_id change and re-anchor to the new account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poseidon.brokers.base import Broker
from poseidon.core.enums import BrokerCapability
from poseidon.core.events import EventBus
from poseidon.core.models import AccountSnapshot, Order, Position
from poseidon.portfolio.state import PortfolioState
from poseidon.portfolio.sync import PortfolioSyncService
from poseidon.storage.db import Database


class _StubBroker(Broker):
    """A live broker whose account_id/equity the test controls."""

    name = "tradier"

    def __init__(self, *, account_id: str, equity: str) -> None:
        super().__init__(credentials={}, paper=False)
        self.account_id = account_id
        self.equity = Decimal(equity)

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset({BrokerCapability.EQUITIES})

    async def connect(self) -> None:
        self._connected = True

    async def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            broker=self.name, account_id=self.account_id, equity=self.equity,
            cash=self.equity, buying_power=self.equity, as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        return []

    async def open_orders(self) -> list[Order]:
        return []

    async def submit_order(self, order: Order) -> Order:
        return order

    async def cancel_order(self, order: Order) -> Order:
        return order

    async def order_status(self, order: Order) -> Order:
        return order


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.open()
    yield database
    await database.close()


async def test_same_scope_account_switch_reanchors_loss_baselines(db) -> None:
    bus = EventBus()
    portfolio = PortfolioState()
    # Account A: a small $10k live account at Tradier.
    broker = _StubBroker(account_id="VA000001", equity="10000")
    sync = PortfolioSyncService(broker, portfolio, bus, db, _clock())
    await sync.restore_baselines()
    await sync.sync_once()
    assert portfolio.day_start_equity == Decimal("10000")

    # Operator connects a DIFFERENT $100k live account at the SAME broker.
    # switch_broker clears the in-memory snapshot then calls restore_baselines,
    # which (scope unchanged) restores account A's $10k anchors — the bug.
    broker.account_id = "VA000002"
    broker.equity = Decimal("100000")
    portfolio.day_start_equity = None
    portfolio.week_start_equity = None
    portfolio.peak_equity = None
    portfolio.day_min_equity = None
    portfolio.week_min_equity = None
    await sync.restore_baselines()

    # The first sync of the new account must re-anchor to $100k, not keep A's
    # $10k anchor (against which a $100k account reads as +$90k and never halts).
    await sync.sync_once()
    assert portfolio.day_start_equity == Decimal("100000")
    assert portfolio.day_loss_pct() == 0.0
    assert await db.kv_get("baseline.account_id") == "VA000002"


async def test_same_account_restart_keeps_baselines(db) -> None:
    bus = EventBus()
    portfolio = PortfolioState()
    broker = _StubBroker(account_id="VA000001", equity="10000")
    sync = PortfolioSyncService(broker, portfolio, bus, db, _clock())
    await sync.restore_baselines()
    await sync.sync_once()
    # A loss on the SAME account must be preserved across a restart-style
    # restore (the account_id gate must not fire when nothing changed).
    broker.equity = Decimal("9500")
    await sync.sync_once()
    assert portfolio.day_loss_pct() >= 0.05 - 1e-9


def _clock():
    from poseidon.core.clock import MarketClock
    return MarketClock()
