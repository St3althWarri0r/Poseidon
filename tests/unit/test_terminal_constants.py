"""Universe/range constants must mirror trading-terminal's lib/constants.ts."""

from __future__ import annotations

from poseidon.terminal.constants import (
    COMMODITIES,
    CRYPTO,
    CURRENCIES,
    FUTURES,
    MAJOR_INDICES,
    RANGE_CONFIG,
    RANGE_KEYS,
    RATES,
    SECTOR_ETFS,
)


def test_universe_sizes_and_spot_symbols() -> None:
    assert [s for s, _ in MAJOR_INDICES] == ["^GSPC", "^DJI", "^IXIC", "^RUT", "^VIX"]
    assert [s for s, _ in FUTURES] == ["ES=F", "NQ=F", "YM=F"]
    assert [s for s, _ in RATES] == ["^TNX", "^FVX", "^TYX"]
    assert [s for s, _ in COMMODITIES] == ["GC=F", "SI=F", "CL=F", "NG=F"]
    assert [s for s, _ in CRYPTO] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert [s for s, _ in CURRENCIES] == ["EURUSD=X", "GBPUSD=X", "JPY=X", "DX-Y.NYB"]
    assert len(SECTOR_ETFS) == 11 and SECTOR_ETFS[0] == ("XLK", "Technology")


def test_range_config_mirrors_ts() -> None:
    assert RANGE_KEYS == ("1D", "5D", "1M", "6M", "YTD", "1Y", "5Y", "MAX")
    assert RANGE_CONFIG["1D"].interval == "5m" and RANGE_CONFIG["1D"].days == 1
    assert RANGE_CONFIG["5D"].interval == "30m" and RANGE_CONFIG["5D"].days == 5
    assert RANGE_CONFIG["YTD"].days == "ytd"
    assert RANGE_CONFIG["5Y"].interval == "1wk" and RANGE_CONFIG["5Y"].days == 1827
    assert RANGE_CONFIG["MAX"].interval == "1mo" and RANGE_CONFIG["MAX"].days == "max"
