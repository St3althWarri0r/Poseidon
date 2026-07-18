"""Core layer: clock, freshness, models, config, event bus."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from poseidon.core.clock import FreshnessPolicy, MarketClock, calendar_covers
from poseidon.core.config import AppConfig, load_config
from poseidon.core.enums import DataFreshness, MarketSession, OrderStatus, TradingMode
from poseidon.core.errors import ConfigError
from poseidon.core.events import EventBus
from poseidon.core.models import Order, OrderSide, Quote

EASTERN = ZoneInfo("America/New_York")


class TestMarketClock:
    clock = MarketClock()

    def test_regular_session(self) -> None:
        at = datetime(2026, 7, 6, 11, 0, tzinfo=EASTERN)  # Monday
        assert self.clock.session(at) is MarketSession.REGULAR

    def test_weekend_closed(self) -> None:
        at = datetime(2026, 7, 4, 11, 0, tzinfo=EASTERN)  # Saturday
        assert self.clock.session(at) is MarketSession.CLOSED

    def test_holiday_closed(self) -> None:
        at = datetime(2026, 7, 3, 11, 0, tzinfo=EASTERN)  # July 4th observed
        assert self.clock.session(at) is MarketSession.CLOSED

    def test_half_day_afternoon_extended_then_closed(self) -> None:
        at = datetime(2026, 11, 27, 14, 0, tzinfo=EASTERN)  # day after Thanksgiving
        assert self.clock.session(at) is MarketSession.AFTER_HOURS
        assert self.clock.session(datetime(2026, 11, 27, 17, 30, tzinfo=EASTERN)) is MarketSession.CLOSED

    def test_pre_and_after_hours(self) -> None:
        assert self.clock.session(datetime(2026, 7, 6, 8, 0, tzinfo=EASTERN)) is MarketSession.PRE_MARKET
        assert self.clock.session(datetime(2026, 7, 6, 17, 0, tzinfo=EASTERN)) is MarketSession.AFTER_HOURS

    def test_unknown_year_fails_safe(self) -> None:
        at = datetime(2031, 7, 7, 11, 0, tzinfo=EASTERN)
        assert not calendar_covers(at.date())
        assert self.clock.session(at) is MarketSession.CLOSED

    def test_next_open_skips_weekend(self) -> None:
        friday_close = datetime(2026, 7, 10, 20, 0, tzinfo=EASTERN)
        nxt = self.clock.next_open(friday_close).astimezone(EASTERN)
        assert nxt.date() == date(2026, 7, 13)  # Monday


class TestFreshness:
    policy = FreshnessPolicy(real_time_max_age=5, delayed_max_age=900)

    def test_grades(self) -> None:
        now = datetime.now(UTC)
        assert self.policy.grade(now) is DataFreshness.REAL_TIME
        assert self.policy.grade(now - timedelta(seconds=60)) is DataFreshness.DELAYED
        assert self.policy.grade(now - timedelta(hours=1)) is DataFreshness.STALE

    def test_naive_timestamp_is_stale(self) -> None:
        assert self.policy.grade(datetime.now()) is DataFreshness.STALE  # noqa: DTZ005


class TestCryptoFreshness:
    # Crypto trades 24/7 and quotes over a public REST endpoint arrive with a
    # looser cadence than a co-located equity feed, so crypto gets its OWN
    # real-time window (default 60s). Equities stay strict at 5s.
    policy = FreshnessPolicy(
        real_time_max_age=5, crypto_real_time_max_age=60, delayed_max_age=900
    )

    def test_crypto_30s_is_real_time_but_equity_30s_is_delayed(self) -> None:
        # The headline invariant: at the SAME 30s age, a crypto quote passes the
        # order gate (REAL_TIME) while an equity quote is rejected (DELAYED).
        now = datetime.now(UTC)
        at_30s = now - timedelta(seconds=30)
        assert self.policy.grade(at_30s, is_crypto=True) is DataFreshness.REAL_TIME
        assert self.policy.grade(at_30s, is_crypto=False) is DataFreshness.DELAYED

    def test_equities_default_is_strict(self) -> None:
        # is_crypto defaults to False — equity callers are unchanged and strict.
        now = datetime.now(UTC)
        assert self.policy.grade(now - timedelta(seconds=6)) is DataFreshness.DELAYED

    def test_crypto_beyond_its_window_is_delayed_then_stale(self) -> None:
        now = datetime.now(UTC)
        assert self.policy.grade(now - timedelta(seconds=90), is_crypto=True) is DataFreshness.DELAYED
        assert self.policy.grade(now - timedelta(hours=1), is_crypto=True) is DataFreshness.STALE

    def test_crypto_naive_timestamp_still_stale(self) -> None:
        # The naive-timestamp safety rule is not weakened for crypto.
        assert self.policy.grade(datetime.now(), is_crypto=True) is DataFreshness.STALE  # noqa: DTZ005


class TestModels:
    def test_quote_mid_and_spread(self) -> None:
        q = Quote(symbol="aapl", bid=Decimal("99.95"), ask=Decimal("100.05"),
                  as_of=datetime.now(UTC), source="t")
        assert q.symbol == "AAPL"
        assert q.mid == Decimal("100.00")
        assert q.spread_pct == Decimal("0.1") / Decimal("100")

    def test_order_rejects_nonpositive_quantity(self) -> None:
        with pytest.raises(ValueError):
            Order(symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("0"))

    def test_order_status_flags(self) -> None:
        assert OrderStatus.FILLED.is_terminal
        assert not OrderStatus.ACCEPTED.is_terminal
        assert OrderStatus.PARTIALLY_FILLED.is_open_at_broker


class TestConfig:
    def test_defaults(self) -> None:
        config = AppConfig()
        assert config.mode is TradingMode.RESEARCH
        assert config.risk.max_daily_loss_pct == 0.03

    def test_data_freshness_defaults(self) -> None:
        # Equities stay strict at 5s; crypto gets its own looser real-time window.
        config = AppConfig()
        assert config.data.real_time_max_age_seconds == 5.0
        assert config.data.crypto_real_time_max_age_seconds == 60.0

    def test_crypto_freshness_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            AppConfig.model_validate({"data": {"crypto_real_time_max_age_seconds": 0}})

    def test_two_primary_brokers_rejected(self) -> None:
        with pytest.raises(ValueError):
            AppConfig.model_validate({
                "brokers": [
                    {"name": "paper", "primary": True},
                    {"name": "alpaca", "primary": True},
                ]
            })

    def test_non_research_requires_primary(self) -> None:
        with pytest.raises(ValueError):
            AppConfig.model_validate({
                "mode": "autonomous",
                "brokers": [{"name": "paper", "primary": False}],
            })

    def test_schedule_requires_exactly_one_trigger(self) -> None:
        with pytest.raises(ValueError):
            AppConfig.model_validate({
                "schedules": [{"name": "x", "job": "review_cycle"}]
            })

    def test_non_loopback_dashboard_requires_token(self) -> None:
        with pytest.raises(ValueError, match="auth_token_credential"):
            AppConfig.model_validate({"dashboard": {"host": "0.0.0.0"}})
        # With a token credential configured it validates.
        AppConfig.model_validate({
            "dashboard": {"host": "0.0.0.0", "auth_token_credential": "dash_token"}
        })

    def test_load_config_env_override(self, tmp_path, monkeypatch) -> None:
        cfg_file = tmp_path / "poseidon.yaml"
        cfg_file.write_text("mode: research\n")
        monkeypatch.setenv("POSEIDON_AI__MODEL", "claude-test-model")
        config = load_config(cfg_file)
        assert config.ai.model == "claude-test-model"

    def test_invalid_yaml_raises_config_error(self, tmp_path) -> None:
        bad = tmp_path / "poseidon.yaml"
        bad.write_text("mode: [unclosed\n")
        with pytest.raises(ConfigError):
            load_config(bad)


class TestEventBus:
    def test_publish_isolates_handler_errors(self) -> None:
        async def scenario() -> list[str]:
            bus = EventBus()
            seen: list[str] = []

            async def bad(_t: str, _p: object) -> None:
                raise RuntimeError("boom")

            async def good(_t: str, payload: object) -> None:
                seen.append(str(payload))

            bus.subscribe("x", bad)
            bus.subscribe("x", good)
            await bus.publish("x", "hello")
            await bus.close()
            return seen

        assert asyncio.run(scenario()) == ["hello"]

    def test_wildcard_subscription(self) -> None:
        async def scenario() -> list[str]:
            bus = EventBus()
            topics: list[str] = []

            async def spy(topic: str, _p: object) -> None:
                topics.append(topic)

            bus.subscribe("*", spy)
            await bus.publish("a")
            await bus.publish("b")
            await bus.close()
            return topics

        assert sorted(asyncio.run(scenario())) == ["a", "b"]
