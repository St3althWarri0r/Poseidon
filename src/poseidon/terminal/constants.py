"""Static reference data mirrored from trading-terminal lib/constants.ts."""

from __future__ import annotations

from typing import NamedTuple

MAJOR_INDICES: tuple[tuple[str, str], ...] = (
    ("^GSPC", "S&P 500"), ("^DJI", "Dow Jones"), ("^IXIC", "Nasdaq"),
    ("^RUT", "Russell 2000"), ("^VIX", "VIX"),
)
FUTURES: tuple[tuple[str, str], ...] = (
    ("ES=F", "S&P Futures"), ("NQ=F", "Nasdaq Fut"), ("YM=F", "Dow Futures"),
)
RATES: tuple[tuple[str, str], ...] = (
    ("^TNX", "US 10Y"), ("^FVX", "US 5Y"), ("^TYX", "US 30Y"),
)
COMMODITIES: tuple[tuple[str, str], ...] = (
    ("GC=F", "Gold"), ("SI=F", "Silver"), ("CL=F", "Crude Oil"), ("NG=F", "Nat Gas"),
)
CRYPTO: tuple[tuple[str, str], ...] = (
    ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
)
CURRENCIES: tuple[tuple[str, str], ...] = (
    ("EURUSD=X", "EUR/USD"), ("GBPUSD=X", "GBP/USD"), ("JPY=X", "USD/JPY"),
    ("DX-Y.NYB", "US Dollar"),
)
SECTOR_ETFS: tuple[tuple[str, str], ...] = (
    ("XLK", "Technology"), ("XLF", "Financials"), ("XLV", "Health Care"),
    ("XLY", "Cons. Disc."), ("XLP", "Cons. Staples"), ("XLE", "Energy"),
    ("XLI", "Industrials"), ("XLB", "Materials"), ("XLU", "Utilities"),
    ("XLRE", "Real Estate"), ("XLC", "Comm. Svcs"),
)


class RangeSpec(NamedTuple):
    interval: str
    days: int | str  # lookback days, or "ytd" / "max"
    label: str


RANGE_CONFIG: dict[str, RangeSpec] = {
    "1D": RangeSpec("5m", 1, "1 Day"),
    "5D": RangeSpec("30m", 5, "5 Days"),
    "1M": RangeSpec("1d", 31, "1 Month"),
    "6M": RangeSpec("1d", 183, "6 Months"),
    "YTD": RangeSpec("1d", "ytd", "Year to Date"),
    "1Y": RangeSpec("1d", 366, "1 Year"),
    "5Y": RangeSpec("1wk", 1827, "5 Years"),
    "MAX": RangeSpec("1mo", "max", "Max"),
}
RANGE_KEYS: tuple[str, ...] = tuple(RANGE_CONFIG)
