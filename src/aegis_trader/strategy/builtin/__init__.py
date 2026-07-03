"""Built-in strategy library.

Every strategy can be independently enabled/disabled in configuration.
Grouped by family: trend (momentum, breakout, swing), reversion
(mean reversion, pairs), rotation (ETF rotation, sector), income/options
(covered calls, cash-secured puts, wheel, protective puts, vertical
spreads, iron condors), volatility regime, and long-horizon (long-term,
dividend, growth) watch strategies.
"""

from __future__ import annotations

from ..base import Strategy
from .longterm import DividendWatchStrategy, GrowthWatchStrategy, LongTermWatchStrategy
from .options_income import (
    CashSecuredPutStrategy,
    CoveredCallStrategy,
    IronCondorStrategy,
    ProtectivePutStrategy,
    VerticalSpreadStrategy,
    WheelStrategy,
)
from .reversion import MeanReversionStrategy, PairsStrategy
from .rotation import EtfRotationStrategy
from .trend import BreakoutStrategy, MomentumStrategy, SwingStrategy
from .volatility import VolatilityRegimeStrategy

BUILTIN_STRATEGIES: dict[str, type[Strategy]] = {
    s.name: s
    for s in (
        MomentumStrategy, BreakoutStrategy, SwingStrategy,
        MeanReversionStrategy, PairsStrategy,
        EtfRotationStrategy,
        CoveredCallStrategy, CashSecuredPutStrategy, WheelStrategy,
        ProtectivePutStrategy, VerticalSpreadStrategy, IronCondorStrategy,
        VolatilityRegimeStrategy,
        LongTermWatchStrategy, DividendWatchStrategy, GrowthWatchStrategy,
    )
}

__all__ = ["BUILTIN_STRATEGIES"]
