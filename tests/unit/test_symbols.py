"""Tests for the crypto symbol detection + normalization helpers (spec task 1)."""

from __future__ import annotations

import pytest

from poseidon.core.enums import AssetClass
from poseidon.core.errors import PoseidonError, UnsupportedSymbolError
from poseidon.core.symbols import (
    asset_class_for_symbol,
    canonical_crypto_pair,
    is_crypto_symbol,
    normalize_crypto_symbol,
)


class TestIsCryptoSymbol:
    @pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD", "btc/usd", " BTC/USD "])
    def test_true_for_crypto_pairs(self, symbol: str) -> None:
        assert is_crypto_symbol(symbol) is True

    @pytest.mark.parametrize("symbol", ["AAPL", "BRK.B", "SPY", "aapl", " AAPL "])
    def test_false_for_equity_tickers(self, symbol: str) -> None:
        assert is_crypto_symbol(symbol) is False

    def test_false_for_double_slash(self) -> None:
        assert is_crypto_symbol("BTC/USD/EUR") is False


class TestAssetClassForSymbol:
    def test_crypto_pair_maps_to_crypto(self) -> None:
        assert asset_class_for_symbol("BTC/USD") is AssetClass.CRYPTO
        assert asset_class_for_symbol("eth/usd") is AssetClass.CRYPTO

    def test_equity_ticker_maps_to_equity(self) -> None:
        assert asset_class_for_symbol("AAPL") is AssetClass.EQUITY
        assert asset_class_for_symbol("BRK.B") is AssetClass.EQUITY


class TestNormalizeCryptoSymbol:
    def test_uppercases_and_strips(self) -> None:
        assert normalize_crypto_symbol(" btc/usd ") == "BTC/USD"
        assert normalize_crypto_symbol("eth/usd") == "ETH/USD"

    def test_already_canonical_is_unchanged(self) -> None:
        assert normalize_crypto_symbol("BTC/USD") == "BTC/USD"

    def test_rejects_stablecoin_quote(self) -> None:
        with pytest.raises(UnsupportedSymbolError):
            normalize_crypto_symbol("BTC/USDT")
        with pytest.raises(UnsupportedSymbolError):
            normalize_crypto_symbol("BTC/USDC")

    def test_rejects_bare_base(self) -> None:
        with pytest.raises(UnsupportedSymbolError):
            normalize_crypto_symbol("BTC")

    def test_error_is_poseidon_error_and_not_retryable(self) -> None:
        with pytest.raises(UnsupportedSymbolError) as exc_info:
            normalize_crypto_symbol("BTC/USDT")
        assert isinstance(exc_info.value, PoseidonError)
        assert exc_info.value.retryable is False


class TestCanonicalCryptoPair:
    """Broker position feeds return crypto pairs slashless (alpaca /v2/positions:
    "USDTUSD") while the platform's canonical form is BASE/USD. One position must
    not split across two ledger keys — that leaves an exit unmatchable by
    reduce-only and a quote unroutable to the crypto provider."""

    def test_slashless_usd_pair_gains_slash(self) -> None:
        assert canonical_crypto_pair("USDTUSD") == "USDT/USD"
        assert canonical_crypto_pair("BTCUSD") == "BTC/USD"

    def test_canonical_form_is_idempotent(self) -> None:
        assert canonical_crypto_pair("USDT/USD") == "USDT/USD"

    def test_lowercase_is_uppercased(self) -> None:
        assert canonical_crypto_pair("ethusd") == "ETH/USD"

    def test_shapes_that_cannot_be_split_pass_through(self) -> None:
        # Never guess: a bare quote, a non-USD suffix, an equity ticker.
        assert canonical_crypto_pair("USD") == "USD"
        assert canonical_crypto_pair("AAPL") == "AAPL"
        assert canonical_crypto_pair("BTCEUR") == "BTCEUR"
