"""Loss/drawdown baselines must not carry across a same-broker account switch.

account_scope is ``name:paper|live`` (no account_id), so connecting a DIFFERENT
real account at the SAME brokerage keeps the same scope string and the baseline
guard would otherwise restore the previous account's day/week loss + drawdown
anchors — mis-anchoring the safety halts. The sync service must detect the
account_id change and re-anchor to the new account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.brokers.base import Broker
from poseidon.core.enums import BrokerCapability
from poseidon.core.events import EventBus
from poseidon.core.models import AccountSnapshot, Order, Position, Transfer
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


class _DepositBroker(_StubBroker):
    """A live broker that reports a one-shot $50k deposit when armed."""

    def __init__(self, *, account_id: str, equity: str) -> None:
        super().__init__(account_id=account_id, equity=equity)
        self.deposit_armed = False

    async def transfers(self, *, since: datetime) -> list[Transfer]:
        if self.deposit_armed:
            self.deposit_armed = False  # one-shot; consumed on the next sync
            # at = since + 1s (deterministic, NOT wall-clock) so it is newer than
            # the flows cursor and gets applied exactly once.
            return [Transfer(id="d1", at=since + timedelta(seconds=1),
                             amount=Decimal("50000"))]
        return []


# F020: an external deposit re-anchors the day/week trough IN MEMORY, but if the
# adjusted trough is not persisted, a same-day restart rebuilds it from raw
# (un-adjusted) equity_marks and reports a phantom drawdown that latches the
# loss/drawdown halts on a healthy account.
async def test_deposit_then_restart_keeps_flow_adjusted_trough(db) -> None:
    bus = EventBus()
    portfolio = PortfolioState()
    broker = _DepositBroker(account_id="VA000001", equity="100000")
    sync = PortfolioSyncService(broker, portfolio, bus, db, _clock())
    await sync.restore_baselines()
    await sync.sync_once()  # day_start=100k, day_min=100k, mark 100k, flows cursor anchored
    assert portfolio.day_start_equity == Decimal("100000")

    # Intraday $10k trading loss.
    broker.equity = Decimal("90000")
    await sync.sync_once()  # trough ratchets to 90k, mark 90k
    assert portfolio.day_min_equity == Decimal("90000")

    # Operator deposits $50k: net +50k re-anchors day_start 100k->150k and the
    # trough 90k->140k in memory (and, post-fix, persists the flow-adjusted trough).
    broker.equity = Decimal("140000")
    broker.deposit_armed = True
    await sync.sync_once()  # applies the flow, mark 140k
    assert portfolio.day_start_equity == Decimal("150000")
    assert portfolio.day_min_equity == Decimal("140000")

    # Same-day restart: fresh in-memory state, restore from the DB.
    portfolio2 = PortfolioState()
    sync2 = PortfolioSyncService(broker, portfolio2, bus, db, _clock())
    await sync2.restore_baselines()

    assert portfolio2.day_start_equity == Decimal("150000")
    # Post-fix: the flow-adjusted trough (140k) is restored from baseline.day.min.
    # Pre-fix: restore rebuilds it from raw marks as MIN(100k,90k,140k)=90k, so
    # day_loss_pct = (150k-90k)/150k = 40% and the loss/drawdown halts latch.
    assert portfolio2.day_min_equity == Decimal("140000"), (
        f"restored trough {portfolio2.day_min_equity} != flow-adjusted 140000 "
        "(pre-fix rebuilds a phantom 90000 from raw marks)"
    )
    assert portfolio2.day_loss_pct() < 0.10  # true ~6.67%, not a phantom 40%
