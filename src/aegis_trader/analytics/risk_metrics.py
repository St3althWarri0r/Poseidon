"""Portfolio risk metrics: historical-simulation VaR, beta, correlation.

What a risk desk watches, computed from the platform's own live bar
history — never from assumed distributions or remembered prices:

  * 1-day historical VaR and expected shortfall (95%/99%) of the current
    book, using each position's actual weight and the joint history of
    daily returns (so cross-correlations are captured for free);
  * portfolio beta to a benchmark (default SPY) over the same window;
  * the most correlated pair of holdings (concentration hiding in plain
    sight);
  * annualized portfolio volatility.

The math is deliberately assumption-light: historical simulation makes no
normality claim, and positions without enough usable history are reported
as *uncovered* rather than silently filled in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ..core.enums import AssetClass
from ..core.errors import DataError
from .regime import RegimeReport, compute_regime

if TYPE_CHECKING:
    from ..data.router import DataRouter
    from ..portfolio.state import PortfolioState

log = structlog.get_logger(__name__)

_WINDOW_BARS = 120  # ~6 months of daily closes
_MIN_OBSERVATIONS = 30


@dataclass
class RiskMetricsReport:
    as_of: datetime
    var_95_pct: float  # 1-day historical VaR as a fraction of equity
    var_99_pct: float
    expected_shortfall_95_pct: float
    annualized_volatility: float
    portfolio_beta: float | None  # None when benchmark history unavailable
    benchmark: str
    max_pairwise_correlation: float | None
    most_correlated_pair: tuple[str, str] | None
    observations: int
    positions_covered: int
    positions_total: int
    uncovered_symbols: list[str]
    regime: RegimeReport | None = None  # benchmark regime, attached by the gatherer

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "var_95_pct": round(self.var_95_pct, 5),
            "var_99_pct": round(self.var_99_pct, 5),
            "expected_shortfall_95_pct": round(self.expected_shortfall_95_pct, 5),
            "annualized_volatility": round(self.annualized_volatility, 4),
            "portfolio_beta": round(self.portfolio_beta, 3) if self.portfolio_beta is not None else None,
            "benchmark": self.benchmark,
            "max_pairwise_correlation": (
                round(self.max_pairwise_correlation, 3)
                if self.max_pairwise_correlation is not None else None
            ),
            "most_correlated_pair": list(self.most_correlated_pair) if self.most_correlated_pair else None,
            "observations": self.observations,
            "positions_covered": self.positions_covered,
            "positions_total": self.positions_total,
            "uncovered_symbols": self.uncovered_symbols,
            "regime": self.regime.as_dict() if self.regime else None,
        }


def _returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile on pre-sorted data (q in [0, 1])."""
    if not sorted_values:
        return 0.0
    idx = q * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a <= 0 or var_b <= 0:
        return None
    return float(cov / (var_a * var_b) ** 0.5)


def compute_risk_metrics(
    weights: dict[str, float],
    returns_by_symbol: dict[str, list[float]],
    benchmark_returns: list[float] | None,
    *,
    benchmark: str,
    positions_total: int,
    uncovered: list[str],
) -> RiskMetricsReport:
    """Pure computation over prepared return series.

    ``weights`` are position market values as fractions of equity (can sum
    to less than 1 — cash — or more — leverage). Series are aligned from
    the most recent observation backwards over the common window.
    """
    covered = {s: r for s, r in returns_by_symbol.items() if s in weights and len(r) >= 2}
    window = min((len(r) for r in covered.values()), default=0)
    portfolio_returns: list[float] = []
    if covered and window >= 2:
        for t in range(window):
            daily = sum(weights[s] * covered[s][len(covered[s]) - window + t] for s in covered)
            portfolio_returns.append(daily)

    var_95 = var_99 = es_95 = ann_vol = 0.0
    if len(portfolio_returns) >= 2:
        ordered = sorted(portfolio_returns)
        var_95 = max(0.0, -_percentile(ordered, 0.05))
        var_99 = max(0.0, -_percentile(ordered, 0.01))
        tail_count = max(1, int(len(ordered) * 0.05))
        es_95 = max(0.0, -sum(ordered[:tail_count]) / tail_count)
        mean = sum(portfolio_returns) / len(portfolio_returns)
        var = sum((r - mean) ** 2 for r in portfolio_returns) / (len(portfolio_returns) - 1)
        ann_vol = float((var ** 0.5) * (252 ** 0.5))

    beta: float | None = None
    if benchmark_returns and len(portfolio_returns) >= 2:
        n = min(len(portfolio_returns), len(benchmark_returns))
        port, bench = portfolio_returns[-n:], benchmark_returns[-n:]
        mean_b = sum(bench) / n
        var_b = sum((x - mean_b) ** 2 for x in bench) / (n - 1) if n > 1 else 0.0
        if var_b > 0:
            mean_p = sum(port) / n
            cov = sum((port[i] - mean_p) * (bench[i] - mean_b) for i in range(n)) / (n - 1)
            beta = cov / var_b

    max_corr: float | None = None
    max_pair: tuple[str, str] | None = None
    symbols = sorted(covered)
    for i, s1 in enumerate(symbols):
        for s2 in symbols[i + 1:]:
            corr = _correlation(covered[s1], covered[s2])
            if corr is not None and (max_corr is None or corr > max_corr):
                max_corr, max_pair = corr, (s1, s2)

    return RiskMetricsReport(
        as_of=datetime.now(UTC),
        var_95_pct=var_95, var_99_pct=var_99, expected_shortfall_95_pct=es_95,
        annualized_volatility=ann_vol,
        portfolio_beta=beta, benchmark=benchmark,
        max_pairwise_correlation=max_corr, most_correlated_pair=max_pair,
        observations=len(portfolio_returns),
        positions_covered=len(covered), positions_total=positions_total,
        uncovered_symbols=sorted(uncovered),
    )


async def gather_risk_metrics(router: DataRouter, portfolio: PortfolioState,
                              *, benchmark: str = "SPY") -> RiskMetricsReport:
    """Fetch live bar history for every equity/ETF position and compute the
    report. Options and symbols without sufficient history are reported as
    uncovered — their risk is NOT estimated (the AI and dashboard see the
    coverage gap explicitly)."""
    equity = float(portfolio.equity) if portfolio.equity else 0.0
    weights: dict[str, float] = {}
    uncovered: list[str] = []
    returns_by_symbol: dict[str, list[float]] = {}
    positions = list(portfolio.positions)
    for position in positions:
        symbol = position.symbol.upper()
        if position.asset_class not in (AssetClass.EQUITY, AssetClass.ETF) or equity <= 0:
            uncovered.append(symbol)
            continue
        value = position.market_value
        if value is None:
            value = position.quantity * position.avg_entry_price
        try:
            bars = await router.bars(symbol, timeframe="1d", limit=_WINDOW_BARS)
        except DataError:
            bars = []
        closes = [float(b.close) for b in bars]
        rets = _returns(closes)
        if len(rets) < _MIN_OBSERVATIONS:
            uncovered.append(symbol)
            continue
        weights[symbol] = float(value) / equity
        returns_by_symbol[symbol] = rets

    benchmark_returns: list[float] | None = None
    benchmark_closes: list[float] = []
    try:
        # 300 bars: enough for the regime's 200-day average AND the ~6-month
        # return window used for beta.
        bench_bars = await router.bars(benchmark, timeframe="1d", limit=300)
        benchmark_closes = [float(b.close) for b in bench_bars]
        benchmark_returns = _returns(benchmark_closes[-_WINDOW_BARS:])
    except DataError:
        log.warning("benchmark history unavailable; beta will be null", benchmark=benchmark)

    report = compute_risk_metrics(
        weights, returns_by_symbol, benchmark_returns,
        benchmark=benchmark, positions_total=len(positions), uncovered=uncovered,
    )
    report.regime = compute_regime(benchmark_closes, benchmark=benchmark)
    return report
