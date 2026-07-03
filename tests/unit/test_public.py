"""Public.com broker plugin and market data provider (no network)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from aegis_trader.brokers.plugins.public_com import PublicBroker, _client_uuid
from aegis_trader.core.enums import (
    AssetClass,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from aegis_trader.core.errors import (
    BrokerAuthError,
    BrokerError,
    ProviderAuthError,
    ProviderError,
)
from aegis_trader.core.models import OptionLeg, Order
from aegis_trader.data.providers.public_data import PublicDataProvider


def make_broker() -> PublicBroker:
    return PublicBroker(credentials={"secret": "s3cret", "account_id": "ACC1"}, paper=False)


class TestPublicBrokerConstruction:
    def test_rejects_paper_mode(self) -> None:
        with pytest.raises(BrokerError, match="no paper environment"):
            PublicBroker(credentials={"secret": "x"}, paper=True)

    def test_requires_secret(self) -> None:
        with pytest.raises(BrokerAuthError):
            PublicBroker(credentials={}, paper=False)

    def test_client_uuid_passthrough_and_fallback(self) -> None:
        canonical = str(uuid.uuid4())
        assert _client_uuid(canonical) == canonical
        assert _client_uuid(canonical.replace("-", "")) == canonical  # hex form
        # Non-UUID ids map deterministically (idempotency preserved on retry).
        a, b = _client_uuid("legacy-42"), _client_uuid("legacy-42")
        assert a == b and str(uuid.UUID(a)) == a


class TestPublicBrokerPayloads:
    def test_single_leg_equity_limit(self) -> None:
        broker = make_broker()
        order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                      quantity=Decimal("10"), limit_price=Decimal("189.50"))
        payload = broker._single_leg_payload(order)
        assert payload["instrument"] == {"symbol": "AAPL", "type": "EQUITY"}
        assert payload["orderSide"] == "BUY"
        assert payload["orderType"] == "LIMIT"
        assert payload["limitPrice"] == "189.50"
        assert payload["quantity"] == "10"
        assert payload["expiration"] == {"timeInForce": "DAY"}
        assert "openCloseIndicator" not in payload
        assert str(uuid.UUID(payload["orderId"]))  # RFC-4122 as Public requires

    def test_single_leg_option_close_indicator(self) -> None:
        broker = make_broker()
        order = Order(symbol="AAPL240621C00190000", asset_class=AssetClass.OPTION,
                      side=OrderSide.SELL_TO_CLOSE, order_type=OrderType.LIMIT,
                      quantity=Decimal("1"), limit_price=Decimal("2.50"))
        payload = broker._single_leg_payload(order)
        assert payload["instrument"]["type"] == "OPTION"
        assert payload["orderSide"] == "SELL"
        assert payload["openCloseIndicator"] == "CLOSE"

    def test_gtc_maps_to_gtd_window(self) -> None:
        broker = make_broker()
        order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                      quantity=Decimal("1"), limit_price=Decimal("100"),
                      time_in_force=TimeInForce.GTC)
        block = broker._expiration_block(order)
        assert block["timeInForce"] == "GTD"
        assert "expirationTime" in block

    def test_extended_hours_session_flag(self) -> None:
        broker = make_broker()
        order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                      quantity=Decimal("1"), limit_price=Decimal("100"), extended_hours=True)
        assert broker._single_leg_payload(order)["equityMarketSession"] == "EXTENDED"

    def test_multileg_requires_limit_price(self) -> None:
        broker = make_broker()
        order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                      quantity=Decimal("1"),
                      legs=[OptionLeg(contract_symbol="AAPL240621C00190000",
                                      side=OrderSide.BUY_TO_OPEN, quantity=1)])
        with pytest.raises(BrokerError, match="LIMIT"):
            broker._multileg_payload(order)

    def test_multileg_payload_shape(self) -> None:
        broker = make_broker()
        order = Order(
            symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=Decimal("2"), limit_price=Decimal("1.25"),
            legs=[
                OptionLeg(contract_symbol="AAPL240621C00190000",
                          side=OrderSide.BUY_TO_OPEN, quantity=1),
                OptionLeg(contract_symbol="AAPL240621C00200000",
                          side=OrderSide.SELL_TO_OPEN, quantity=1),
            ],
        )
        payload = broker._multileg_payload(order)
        assert payload["type"] == "LIMIT" and payload["quantity"] == 2
        assert [leg["side"] for leg in payload["legs"]] == ["BUY", "SELL"]
        assert all(leg["openCloseIndicator"] == "OPEN" for leg in payload["legs"])
        assert all(leg["instrument"]["type"] == "OPTION" for leg in payload["legs"])


class TestPublicBrokerParsing:
    PORTFOLIO: dict[str, Any] = {
        "buyingPower": {"buyingPower": "5000.25", "optionsBuyingPower": "2500"},
        "equity": [
            {"type": "CASH", "value": "5000.25"},
            {"type": "STOCKS", "value": "10000"},
        ],
        "positions": [
            {
                "instrument": {"symbol": "AAPL", "type": "EQUITY"},
                "quantity": "50",
                "currentValue": "10000",
                "costBasis": {"unitCost": "180.00", "gainValue": "1000"},
            },
            {"instrument": {"symbol": "GONE", "type": "EQUITY"}, "quantity": "0"},
        ],
        "orders": [
            {
                "orderId": "11111111-1111-4111-8111-111111111111",
                "instrument": {"symbol": "MSFT", "type": "EQUITY"},
                "side": "BUY", "type": "LIMIT", "status": "NEW",
                "quantity": "5", "limitPrice": "400.00",
                "expiration": {"timeInForce": "DAY"},
            },
            {   # terminal orders are filtered out of open_orders()
                "orderId": "22222222-2222-4222-8222-222222222222",
                "instrument": {"symbol": "AAPL", "type": "EQUITY"},
                "side": "SELL", "type": "MARKET", "status": "FILLED",
                "quantity": "1",
            },
        ],
    }

    @pytest.fixture()
    def broker(self, monkeypatch: pytest.MonkeyPatch) -> PublicBroker:
        broker = make_broker()

        async def fake_portfolio() -> dict[str, Any]:
            return self.PORTFOLIO

        monkeypatch.setattr(broker, "_portfolio", fake_portfolio)
        return broker

    async def test_account_snapshot(self, broker: PublicBroker) -> None:
        snapshot = await broker.account()
        assert snapshot.equity == Decimal("15000.25")
        assert snapshot.cash == Decimal("5000.25")
        assert snapshot.buying_power == Decimal("5000.25")
        assert snapshot.options_buying_power == Decimal("2500")

    async def test_positions_skip_zero_quantity(self, broker: PublicBroker) -> None:
        positions = await broker.positions()
        assert [p.symbol for p in positions] == ["AAPL"]
        assert positions[0].avg_entry_price == Decimal("180.00")
        assert positions[0].unrealized_pnl == Decimal("1000")

    async def test_open_orders_filters_terminal(self, broker: PublicBroker) -> None:
        orders = await broker.open_orders()
        assert [o.symbol for o in orders] == ["MSFT"]
        assert orders[0].status is OrderStatus.ACCEPTED
        assert orders[0].limit_price == Decimal("400.00")

    async def test_submit_order_sets_broker_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broker = make_broker()
        seen: dict[str, Any] = {}

        async def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
            seen["method"], seen["url"] = method, url
            seen["payload"] = kwargs.get("json_body")
            return {"orderId": seen["payload"]["orderId"]}

        async def fake_headers() -> dict[str, str]:
            return {}

        monkeypatch.setattr(broker, "_request", fake_request)
        monkeypatch.setattr(broker, "_auth_headers", fake_headers)
        order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                      quantity=Decimal("1"), limit_price=Decimal("100"))
        result = await broker.submit_order(order)
        assert seen["method"] == "POST" and seen["url"].endswith("/trading/ACC1/order")
        assert result.status is OrderStatus.SUBMITTED
        assert result.broker_order_id == seen["payload"]["orderId"]


class TestPublicDataProvider:
    def make_provider(self) -> PublicDataProvider:
        provider = PublicDataProvider(
            api_key="s3cret", options={"account_id": "ACC1", "crypto_symbols": ["BTC"]}
        )
        provider._access_token = "tok"
        provider._token_expiry = 1e12  # never refreshes inside a test
        return provider

    def test_requires_api_key(self) -> None:
        with pytest.raises(ProviderAuthError):
            PublicDataProvider(api_key="")

    def test_instrument_type_inference(self) -> None:
        provider = self.make_provider()
        assert provider._instrument("aapl") == {"symbol": "AAPL", "type": "EQUITY"}
        assert provider._instrument("btc") == {"symbol": "BTC", "type": "CRYPTO"}

    async def test_quote_parses_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = self.make_provider()
        now = datetime(2026, 7, 2, 15, 30, tzinfo=UTC)

        async def fake_post(url: str, *, json_body: Any, headers: Any = None) -> Any:
            assert url.endswith("/marketdata/ACC1/quotes")
            assert json_body == {"instruments": [{"symbol": "AAPL", "type": "EQUITY"}]}
            return {"quotes": [{
                "instrument": {"symbol": "AAPL", "type": "EQUITY"},
                "outcome": "SUCCESS",
                "last": "190.12", "lastTimestamp": now.isoformat(),
                "bid": "190.10", "bidSize": 4, "ask": "190.14", "askSize": 2,
                "volume": 1000000,
            }]}

        monkeypatch.setattr(provider, "_post_json", fake_post)
        quote = await provider.quote("AAPL")
        assert quote.bid == Decimal("190.10") and quote.ask == Decimal("190.14")
        assert quote.as_of == now and quote.source == "public_data"

    async def test_quote_failure_outcome_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = self.make_provider()

        async def fake_post(url: str, *, json_body: Any, headers: Any = None) -> Any:
            return {"quotes": [{"instrument": {"symbol": "ZZZZ", "type": "EQUITY"},
                                "outcome": "UNKNOWN"}]}

        monkeypatch.setattr(provider, "_post_json", fake_post)
        with pytest.raises(ProviderError, match="no quote"):
            await provider.quote("ZZZZ")

    async def test_bars_parse_and_timeframe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = self.make_provider()

        async def fake_get(url: str, *, params: Any = None, headers: Any = None) -> Any:
            assert "/historicdata/EQUITY/AAPL/YEAR/ONE_DAY" in url
            return {"regularMarket": {"expectedBars": 2, "bars": [
                {"timestamp": "2026-07-01T20:00:00Z", "open": "188", "high": "191",
                 "low": "187", "close": "190", "value": "190", "volume": "52000000"},
                {"timestamp": "2026-07-02T20:00:00Z", "open": "190", "high": "192",
                 "low": "189", "close": "191", "value": "191", "volume": "48000000"},
            ]}}

        monkeypatch.setattr(provider, "_get_json", fake_get)
        bars = await provider.bars("AAPL", timeframe="1d", limit=10)
        assert len(bars) == 2
        assert bars[-1].close == Decimal("191") and bars[-1].volume == 48000000

    async def test_bars_unknown_timeframe(self) -> None:
        provider = self.make_provider()
        with pytest.raises(ProviderError, match="unsupported timeframe"):
            await provider.bars("AAPL", timeframe="3m", limit=5)

    async def test_option_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = self.make_provider()
        expiry = date(2026, 7, 17)

        def quote_row(symbol: str, strike: str, outcome: str = "SUCCESS") -> dict[str, Any]:
            return {
                "instrument": {"symbol": symbol, "type": "OPTION"},
                "outcome": outcome, "bid": "2.40", "ask": "2.60", "last": "2.50",
                "volume": 100, "openInterest": 5000,
                "optionDetails": {
                    "strikePrice": strike,
                    "greeks": {"delta": "0.55", "impliedVolatility": "0.28"},
                },
            }

        async def fake_post(url: str, *, json_body: Any, headers: Any = None) -> Any:
            assert url.endswith("/marketdata/ACC1/option-chain")
            assert json_body["expirationDate"] == "2026-07-17"
            return {
                "baseSymbol": "AAPL",
                "calls": [quote_row("AAPL260717C00190000", "190"),
                          quote_row("AAPL260717C00200000", "200", outcome="UNKNOWN")],
                "puts": [quote_row("AAPL260717P00180000", "180")],
            }

        monkeypatch.setattr(provider, "_post_json", fake_post)
        chain = await provider.option_chain("AAPL", expiration=expiry)
        assert chain.expirations == [expiry]
        assert len(chain.contracts) == 2  # UNKNOWN outcome dropped
        call = next(c for c in chain.contracts if c.right is OptionRight.CALL)
        assert call.strike == Decimal("190")
        assert call.greeks is not None and call.greeks.delta == 0.55
        put = next(c for c in chain.contracts if c.right is OptionRight.PUT)
        assert put.symbol == "AAPL260717P00180000"
