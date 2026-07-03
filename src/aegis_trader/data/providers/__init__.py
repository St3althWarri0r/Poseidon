"""Built-in market data providers.

Each module implements :class:`~aegis_trader.data.base.MarketDataProvider`
against the provider's official public REST API. Third-party providers can
be added through the ``aegis_trader.data_providers`` entry-point group
(see docs/plugin-development.md).
"""

from __future__ import annotations

from ..base import MarketDataProvider
from .alpaca_data import AlpacaDataProvider
from .alphavantage import AlphaVantageProvider
from .finnhub import FinnhubProvider
from .polygon import PolygonProvider
from .public_data import PublicDataProvider
from .tradier_data import TradierDataProvider
from .twelvedata import TwelveDataProvider

BUILTIN_PROVIDERS: dict[str, type[MarketDataProvider]] = {
    PolygonProvider.name: PolygonProvider,
    FinnhubProvider.name: FinnhubProvider,
    TwelveDataProvider.name: TwelveDataProvider,
    AlphaVantageProvider.name: AlphaVantageProvider,
    AlpacaDataProvider.name: AlpacaDataProvider,
    TradierDataProvider.name: TradierDataProvider,
    PublicDataProvider.name: PublicDataProvider,
}

__all__ = ["BUILTIN_PROVIDERS"]
