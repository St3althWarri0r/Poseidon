"""Account view backend (broker catalog, config overlay, broker-switch
guards) and the AI Desk chat service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from poseidon.ai.chat import ChatService
from poseidon.ai.schemas import DATA_TOOLS
from poseidon.brokers.plugins.paper import PaperBroker
from poseidon.brokers.registry import broker_catalog
from poseidon.core.clock import FreshnessPolicy, MarketClock
from poseidon.core.config import AIConfig, RiskConfig, apply_local_overlay, load_config
from poseidon.core.enums import DataFreshness, OrderSide, OrderStatus, TradingMode
from poseidon.core.errors import StaleDataError
from poseidon.core.events import EventBus
from poseidon.core.models import Order
from poseidon.data.router import DataRouter
from poseidon.execution.approvals import ApprovalQueue
from poseidon.execution.manager import OrderManager
from poseidon.portfolio.state import PortfolioState
from poseidon.risk.engine import RiskEngine
from poseidon.security.audit import AuditLog
from poseidon.storage.db import Database

from ..conftest import FakeProvider

# ---------------------------------------------------------------- catalog


def test_catalog_paper_first_and_shapes() -> None:
    catalog = broker_catalog()
    assert catalog[0]["name"] == "paper"
    connectable = [e for e in catalog if e["connectable"]]
    stubs = [e for e in catalog if not e["connectable"]]
    assert {e["name"] for e in connectable} >= {
        "paper", "alpaca", "tradier", "tastytrade", "schwab", "ibkr", "public"}
    for entry in connectable:
        assert entry["paper_choice"] in ("toggle", "live_only", "always")
        assert isinstance(entry["fields"], list)
        for f in entry["fields"]:
            assert f["key"] and f["label"]
    # Stubs carry their documented reason so the UI can explain WHY.
    assert {e["name"] for e in stubs} >= {"fidelity", "vanguard", "m1finance", "robinhood"}
    for entry in stubs:
        assert entry["stub_reason"]


def test_catalog_live_only_brokers() -> None:
    by_name = {e["name"]: e for e in broker_catalog()}
    assert by_name["public"]["paper_choice"] == "live_only"
    assert by_name["public"]["credential"] == "public_api_secret"
    assert by_name["schwab"]["paper_choice"] == "live_only"


# ---------------------------------------------------------------- overlay


def test_apply_local_overlay_merges_brokers_by_name() -> None:
    base = {
        "brokers": [{"name": "paper", "enabled": True, "primary": True}],
        "data": {"providers": [{"name": "finnhub", "credential": "finnhub_api_key"}]},
    }
    overlay = {
        "brokers": [{"name": "public", "enabled": True, "primary": True,
                     "credential": "public_api_secret", "paper": False}],
        "data": {"providers": [{"name": "public_data",
                                "credential": "public_api_secret", "priority": 10}]},
    }
    merged = apply_local_overlay(base, overlay)
    brokers = {b["name"]: b for b in merged["brokers"]}
    # The overlay's primary wins; the base primary is demoted, not dropped.
    assert brokers["paper"]["primary"] is False
    assert brokers["public"]["primary"] is True
    providers = {p["name"] for p in merged["data"]["providers"]}
    assert providers == {"finnhub", "public_data"}


def test_apply_local_overlay_replaces_same_name_entry() -> None:
    base = {"brokers": [{"name": "alpaca", "enabled": True, "primary": True, "paper": True}]}
    overlay = {"brokers": [{"name": "alpaca", "enabled": True, "primary": True, "paper": False}]}
    merged = apply_local_overlay(base, overlay)
    assert merged["brokers"] == [
        {"name": "alpaca", "enabled": True, "primary": True, "paper": False}]


def test_load_config_reads_dashboard_overlay(tmp_path) -> None:
    main = tmp_path / "poseidon.yaml"
    main.write_text(yaml.safe_dump({
        "mode": "research",
        "brokers": [{"name": "paper", "enabled": True, "primary": True}],
    }))
    (tmp_path / "poseidon.local.yaml").write_text(yaml.safe_dump({
        "brokers": [{"name": "alpaca", "enabled": True, "primary": True,
                     "credential": "alpaca_keys", "paper": True}],
    }))
    config = load_config(main)
    primary = config.primary_broker()
    assert primary is not None and primary.name == "alpaca"
    assert config.config_path == main
    names = {b.name for b in config.brokers}
    assert names == {"paper", "alpaca"}


def test_load_config_without_overlay_unchanged(tmp_path) -> None:
    main = tmp_path / "poseidon.yaml"
    main.write_text(yaml.safe_dump({
        "brokers": [{"name": "paper", "enabled": True, "primary": True}]}))
    config = load_config(main)
    primary = config.primary_broker()
    assert primary is not None and primary.name == "paper"


# ------------------------------------------------------- broker-switch guards


@pytest.fixture
async def manager(tmp_path):
    bus = EventBus()
    router = DataRouter([(FakeProvider(name="feed"), 10)], FreshnessPolicy())
    broker = PaperBroker(credentials={}, options={
        "starting_cash": "100000", "state_file": str(tmp_path / "paper.json")})
    broker.set_quote_fn(lambda s: router.quote(s, allow_delayed=True))
    await broker.connect()
    db = Database(tmp_path / "t.db")
    await db.open()
    risk = RiskEngine(RiskConfig(), PortfolioState(), router, MarketClock(), bus)
    mgr = OrderManager(broker, risk, ApprovalQueue(bus), db, AuditLog(db), bus,
                       mode=TradingMode.AUTONOMOUS)
    yield mgr, db, broker
    await bus.close()
    await db.close()


async def test_open_order_count_and_set_broker(manager) -> None:
    mgr, db, broker = manager
    assert await mgr.open_order_count() == 0
    order = Order(symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("5"),
                  limit_price=Decimal("100"), status=OrderStatus.ACCEPTED,
                  broker="paper", created_at=datetime.now(UTC))
    await mgr._persist(order)
    assert await mgr.open_order_count() == 1
    assert mgr.broker_name == "paper"


async def test_switching_refuses_new_orders_and_drains(manager) -> None:
    # While a broker switch is in progress every new order pipeline must be
    # refused — an order decided against one account must never reach another.
    mgr, db, broker = manager
    await mgr.begin_broker_switch(timeout=1)
    order = Order(symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("1"),
                  limit_price=Decimal("100"))
    result = await mgr.submit_manual(order)
    assert result.status is OrderStatus.REJECTED_RISK
    assert "switch in progress" in (result.status_reason or "")
    mgr.end_broker_switch()


async def test_switch_drain_times_out_with_inflight_pipeline(manager) -> None:
    mgr, db, broker = manager
    mgr._pipeline_enter()  # simulate an order mid-pipeline
    with pytest.raises(Exception, match="in flight"):
        await mgr.begin_broker_switch(timeout=0.05)
    assert mgr._switching is False  # refusal lifted after the failed switch
    mgr._pipeline_exit()


def test_broker_account_scope_separates_paper_and_live(tmp_path) -> None:
    # alpaca-paper and alpaca-live are different accounts: their equity
    # histories must never share a persistence key.
    paper = PaperBroker(credentials={}, options={"state_file": str(tmp_path / "p.json")})
    assert paper.account_scope == "paper:paper"
    assert ":" in paper.account_scope


def test_merge_preserves_base_only_keys() -> None:
    # An overlay row must deep-merge over the base row: yaml-configured
    # options (e.g. ibkr gateway_url) survive a dashboard-written overlay.
    base = {"brokers": [{"name": "ibkr", "enabled": True, "primary": True,
                         "options": {"gateway_url": "https://localhost:5000"}}]}
    overlay = {"brokers": [{"name": "ibkr", "enabled": True, "primary": True, "paper": False,
                            "credential": "ibkr_creds"}]}
    merged = apply_local_overlay(base, overlay)
    entry = merged["brokers"][0]
    assert entry["options"] == {"gateway_url": "https://localhost:5000"}
    assert entry["paper"] is False and entry["credential"] == "ibkr_creds"


async def test_resume_orphans_orders_from_another_broker(manager) -> None:
    # An order left open at broker A must NOT be polled against broker B —
    # its ids mean nothing there. It is marked ERROR with an explanation.
    mgr, db, broker = manager
    order = Order(symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("5"),
                  limit_price=Decimal("100"), status=OrderStatus.SUBMITTED,
                  broker="alpaca", broker_order_id="abc123",
                  created_at=datetime.now(UTC))
    await mgr._persist(order)
    resumed = await mgr.resume_open_orders()
    assert resumed == 0
    row = await db.fetch_one("SELECT status FROM orders WHERE id = ?", (order.id,))
    assert row is not None and row[0] == OrderStatus.ERROR.value


# ------------------------------------------------------------------ chat


def _text_response(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                              cache_read_input_tokens=1, cache_creation_input_tokens=2),
    )


def _tool_response(name: str, tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=name, input=tool_input, id="tu1")],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


class _StubClient:
    def __init__(self, responses: list) -> None:
        self.messages = self
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _StubDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def dispatch(self, name: str, tool_input: dict) -> tuple[str, bool]:
        self.dispatched.append(name)
        return '{"ok": true}', False


@pytest.fixture
async def chat_db(tmp_path):
    db = Database(tmp_path / "chat.db")
    await db.open()
    yield db
    await db.close()


async def test_chat_plain_reply_persists_history(chat_db) -> None:
    client = _StubClient([_text_response("AAPL last printed $210.30 (finnhub).")])
    chat = ChatService(AIConfig(), client, _StubDispatcher(), chat_db)  # type: ignore[arg-type]
    result = await chat.send("how is AAPL?", context="mode: research")
    assert "210.30" in result["reply"]
    assert result["usage"]["api_calls"] == 1
    history = await chat.history()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "how is AAPL?"  # context is NOT persisted


async def test_chat_tool_loop_dispatches_then_replies(chat_db) -> None:
    client = _StubClient([
        _tool_response("get_quote", {"symbol": "AAPL"}),
        _text_response("Fetched it."),
    ])
    dispatcher = _StubDispatcher()
    chat = ChatService(AIConfig(), client, dispatcher, chat_db)  # type: ignore[arg-type]
    result = await chat.send("quote AAPL", context="c")
    assert dispatcher.dispatched == ["get_quote"]
    assert result["tool_calls"] == ["get_quote"]
    assert result["reply"] == "Fetched it."
    assert result["usage"]["api_calls"] == 2


async def test_chat_context_attached_but_not_stored(chat_db) -> None:
    client = _StubClient([_text_response("ok")])
    chat = ChatService(AIConfig(), client, _StubDispatcher(), chat_db)  # type: ignore[arg-type]
    await chat.send("hello", context="equity: 100000")
    # First message of the first call is this send's user turn (the list
    # object mutates in the loop afterwards, so index from the front).
    sent = client.calls[0]["messages"][0]["content"]
    assert "equity: 100000" in sent and "hello" in sent


async def test_chat_clear(chat_db) -> None:
    client = _StubClient([_text_response("ok")])
    chat = ChatService(AIConfig(), client, _StubDispatcher(), chat_db)  # type: ignore[arg-type]
    await chat.send("hello", context="c")
    await chat.clear()
    assert await chat.history() == []


def test_chat_tools_can_never_trade() -> None:
    # The chat offers DATA_TOOLS only: submit_decision must not be present.
    names = {t["name"] for t in DATA_TOOLS}
    assert "submit_decision" not in names
    assert {"get_quote", "get_bars", "get_portfolio", "get_risk_status"} <= names


# ------------------------------------------------------------ reference quote


async def test_reference_quote_returns_stale_without_raising() -> None:
    router = DataRouter([(FakeProvider(name="old", stale=True), 10)],
                        FreshnessPolicy(real_time_max_age=5.0, delayed_max_age=900.0))
    with pytest.raises(StaleDataError):
        await router.quote("AAPL", allow_delayed=True)
    quote = await router.reference_quote("AAPL")
    assert quote.freshness is DataFreshness.STALE
    assert quote.last is not None
