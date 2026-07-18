# tests/unit/test_screener_config.py
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from poseidon.core.config import AppConfig, ScreenerConfig


def test_defaults_disabled() -> None:
    c = AppConfig().screener
    assert isinstance(c, ScreenerConfig)
    assert c.enabled is False  # OFF by default — zero behavior change
    assert c.universe == "sp500"
    assert c.top_n == 15
    assert c.min_dollar_volume == Decimal("20000000")
    assert c.refresh_minutes == 15
    assert c.bars_limit == 90
    assert c.max_batch_symbols == 200


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
