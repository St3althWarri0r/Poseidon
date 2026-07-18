# tests/unit/test_screener_config.py
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from poseidon.core.config import (
    AppConfig,
    CryptoScreenerConfig,
    ScreenerConfig,
    ScreenerConfigBase,
)


def test_defaults_disabled() -> None:
    c = AppConfig().screener
    assert isinstance(c, ScreenerConfig)
    assert isinstance(c, ScreenerConfigBase)  # equity screener is a base subclass
    assert c.enabled is False  # OFF by default — zero behavior change
    assert c.universe == "sp500"
    assert c.top_n == 15
    assert c.min_dollar_volume == Decimal("20000000")
    assert c.refresh_minutes == 15
    assert c.bars_limit == 90
    assert c.max_batch_symbols == 200


def test_crypto_defaults() -> None:
    c = AppConfig().crypto_screener
    assert isinstance(c, CryptoScreenerConfig)
    assert isinstance(c, ScreenerConfigBase)  # reuses the shared base, not a duplicate
    # OFF by default IN CODE — the user opts in via their own config (safety invariant).
    assert c.enabled is False
    assert c.universe == "crypto"
    assert c.top_n == 10
    assert c.min_dollar_volume == Decimal("10000000")  # $10M median 20d ADV$
    assert c.refresh_minutes == 15
    assert c.bars_limit == 90
    assert c.concurrency == 6  # bounded Coinbase fan-out


def test_crypto_concurrency_bounds() -> None:
    assert CryptoScreenerConfig(concurrency=1).concurrency == 1
    assert CryptoScreenerConfig(concurrency=20).concurrency == 20
    with pytest.raises(ValidationError):
        CryptoScreenerConfig(concurrency=0)
    with pytest.raises(ValidationError):
        CryptoScreenerConfig(concurrency=21)


def test_crypto_universe_is_fixed_literal() -> None:
    with pytest.raises(ValidationError):
        CryptoScreenerConfig(universe="sp500")  # type: ignore[arg-type]


def test_crypto_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        CryptoScreenerConfig(bogus=True)  # type: ignore[call-arg]


def test_top_n_bounds() -> None:
    assert ScreenerConfig(top_n=1).top_n == 1
    assert ScreenerConfig(top_n=100).top_n == 100
    with pytest.raises(ValidationError):
        ScreenerConfig(top_n=0)
    with pytest.raises(ValidationError):
        ScreenerConfig(top_n=101)


def test_min_dollar_volume_is_decimal() -> None:
    c = ScreenerConfig(min_dollar_volume=Decimal("5000000"))
    assert isinstance(c.min_dollar_volume, Decimal)
    assert c.min_dollar_volume == Decimal("5000000")


def test_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        ScreenerConfig(bogus=True)  # type: ignore[call-arg]
