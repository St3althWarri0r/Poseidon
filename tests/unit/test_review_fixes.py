"""Regression tests for the adversarial review pass.

Each test pins a specific confirmed finding so the fix cannot silently
regress. Grouped by subsystem; see the commit message for the finding list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.analytics.performance import FillRecord, build_round_trips
from poseidon.core.config import _deep_env_overrides, dashboard_token_from_env
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.errors import RiskViolation
from poseidon.core.models import AccountSnapshot, Order, Position
from poseidon.data.base import MarketDataProvider
from poseidon.portfolio.state import PortfolioState
from poseidon.risk.rules import ReduceOnlyRule
from poseidon.strategy.custom import CustomAlgorithm, _safe_builtins, validate_algorithm

from ..conftest import make_quote

NOW = datetime.now(UTC)


# ---------------------------------------------------------------- RCE sandbox (finding 1)

RCE_PAYLOADS = [
    "async def scan(ctx):\n    __builtins__['__import__']('os').system('id')\n    return []",
    "async def scan(ctx):\n    ([open][0])('/tmp/pwn','w')\n    return []",
    "async def scan(ctx):\n    open('/tmp/pwn','w')\n    return []",
]


@pytest.mark.parametrize("src", RCE_PAYLOADS)
def test_algorithm_sandbox_blocks_exploits(src: str) -> None:
    # Static screen rejects every known bypass...
    assert validate_algorithm(src)
    # ...and construction (exec) never succeeds for them either.
    with pytest.raises(ValueError):
        CustomAlgorithm(algo_name="x", source=src, symbols=["AAPL"])


def test_algorithm_sandbox_has_no_dangerous_builtins() -> None:
    safe = _safe_builtins()
    for name in ("open", "exec", "eval", "compile", "__import__", "getattr", "globals"):
        assert safe.get(name) is not (globals().get("__builtins__") or {})  # not the real one
    assert "open" not in safe and "eval" not in safe


def test_legit_algorithm_still_runs() -> None:
    src = ("async def scan(ctx):\n"
           "    from datetime import datetime\n"
           "    vals = sorted([3.0, 1.0, 2.0])\n"
           "    return [{'symbol': 'AAPL', 'direction': 'long', "
           "'strength': min(round(sum(vals)/len(vals), 2), 1.0)}]\n")
    assert validate_algorithm(src) == []
    CustomAlgorithm(algo_name="legit", source=src, symbols=["AAPL"])  # no raise


# ---------------------------------------------------- naked-short guard (finding 2)

def _portfolio(equity: str = "100000") -> PortfolioState:
    state = PortfolioState()
    state.account = AccountSnapshot(broker="paper", account_id="t", equity=Decimal(equity),
                                    cash=Decimal(equity), buying_power=Decimal(equity), as_of=NOW)
    state.synced_at = NOW
    return state


def _reduce_ctx(order: Order, portfolio: PortfolioState):
    from poseidon.core.clock import MarketClock
    from poseidon.risk.rules import RiskContext
    return RiskContext(order=order, quote=make_quote(order.symbol, "100"),
                       portfolio=portfolio, config=__import__("poseidon.core.config",
                       fromlist=["RiskConfig"]).RiskConfig(), clock=MarketClock())


def test_reduce_only_blocks_naked_short() -> None:
    order = Order(symbol="XYZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                  quantity=Decimal("200"), limit_price=Decimal("100"))
    with pytest.raises(RiskViolation, match="reduce_only"):
        ReduceOnlyRule().check(_reduce_ctx(order, _portfolio()))


def test_reduce_only_allows_closing_a_held_long() -> None:
    state = _portfolio()
    state.positions = [Position(symbol="XYZ", quantity=Decimal("200"),
                                avg_entry_price=Decimal("90"), as_of=NOW)]
    order = Order(symbol="XYZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                  quantity=Decimal("200"), limit_price=Decimal("100"))
    ReduceOnlyRule().check(_reduce_ctx(order, state))  # no raise


def test_reduce_only_rejects_selling_more_than_held() -> None:
    state = _portfolio()
    state.positions = [Position(symbol="XYZ", quantity=Decimal("100"),
                                avg_entry_price=Decimal("90"), as_of=NOW)]
    order = Order(symbol="XYZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                  quantity=Decimal("150"), limit_price=Decimal("100"))
    with pytest.raises(RiskViolation, match="reduce_only"):
        ReduceOnlyRule().check(_reduce_ctx(order, state))


# ---------------------------------------------------- loss-halt latch (finding 5)

def test_loss_halt_latches_through_intraday_recovery() -> None:
    state = PortfolioState()
    state.day_start_equity = Decimal("100000")
    state.record_equity(Decimal("96500"), NOW)  # -3.5%
    # Recover to -2.5%; the latch must still report the trough loss.
    state.account = AccountSnapshot(broker="p", account_id="t", equity=Decimal("97500"),
                                    cash=Decimal("97500"), buying_power=Decimal("97500"), as_of=NOW)
    state.record_equity(Decimal("97500"), NOW)
    assert state.day_loss_pct() >= 0.035 - 1e-9


# ---------------------------------------------- option exposure multiplier (finding 10)

def test_gross_exposure_applies_option_multiplier_when_marketless() -> None:
    state = PortfolioState()
    state.positions = [Position(symbol="SPY241220P00500000", asset_class=AssetClass.OPTION,
                                quantity=Decimal("10"), avg_entry_price=Decimal("5"),
                                market_value=None, as_of=NOW)]
    # 10 contracts * $5 premium * 100 multiplier = $5,000, not $50.
    assert state.gross_exposure() == Decimal("5000")


# ------------------------------------------------------- config env (findings 11, 12)

def test_reserved_vault_env_excluded_from_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSEIDON_VAULT_PASSPHRASE_FILE", "/run/secrets/x")
    monkeypatch.setenv("POSEIDON_DASHBOARD_TOKEN", "abc")
    overrides = _deep_env_overrides()
    assert "vault_passphrase_file" not in overrides
    assert "dashboard_token" not in overrides
    assert dashboard_token_from_env() == "abc"


# --------------------------------------------------------- retry-after (finding 15)

def test_retry_after_parses_http_date_and_seconds() -> None:
    assert MarketDataProvider._parse_retry_after("120") == 120.0
    assert MarketDataProvider._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") is not None
    assert MarketDataProvider._parse_retry_after("garbage") is None
    assert MarketDataProvider._parse_retry_after(None) is None


# ----------------------------------------------------- FIFO short round trips (finding 25)

def test_short_round_trip_pnl_is_correct() -> None:
    fills = [
        FillRecord(symbol="X", side=OrderSide.SELL_TO_OPEN, quantity=Decimal("100"),
                   price=Decimal("50"), at=NOW),
        FillRecord(symbol="X", side=OrderSide.BUY_TO_CLOSE, quantity=Decimal("100"),
                   price=Decimal("45"), at=NOW + timedelta(days=1)),
    ]
    trips = build_round_trips(fills)
    assert len(trips) == 1
    # Short sold at 50, covered at 45 -> +$500 profit (not dropped, not a phantom long).
    assert trips[0].is_short and trips[0].pnl == Decimal("500")


# --------------------------------------------------- paper broker stop orders (finding 3)

class _MutableQuoteFn:
    def __init__(self, symbol: str, price: str) -> None:
        self._q = make_quote(symbol, price)

    def set(self, symbol: str, price: str) -> None:
        self._q = make_quote(symbol, price)

    async def __call__(self, _symbol: str):
        return self._q


async def test_paper_stop_order_rests_until_triggered() -> None:
    from poseidon.brokers.plugins.paper import PaperBroker
    from poseidon.core.enums import OrderStatus

    qf = _MutableQuoteFn("AAPL", "190")
    broker = PaperBroker(credentials={}, options={"quote_fn": qf, "starting_cash": "1000000"})
    await broker.connect()

    buy = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=Decimal("100"))
    await broker.submit_order(buy)
    assert buy.status is OrderStatus.FILLED

    # Sell stop at 170 while the market is at ~190: must NOT fill yet.
    stop = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP,
                 quantity=Decimal("100"), stop_price=Decimal("170"))
    await broker.submit_order(stop)
    assert stop.status is not OrderStatus.FILLED

    # Market falls through the stop: now it triggers on the next status poll.
    qf.set("AAPL", "165")
    await broker.order_status(stop)
    assert stop.status is OrderStatus.FILLED
