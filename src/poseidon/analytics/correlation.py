"""Cross-asset correlation matrix: date-aligned daily-return pearson/spearman.

An advisory concentration lens over the whole requested set — the full NxN
view the risk report's single most-correlated-pair scalar cannot show. Every
pair is aligned on its INTERSECTED close dates (crypto trades 7 days, equities
5 — returns are measured over identical calendar spans for both legs), and a
pair without ``min_overlap`` shared observations reports an honest ``None``
cell plus its measured observation count, never a guess.

Floats throughout: this mirrors ``analytics/risk_metrics.py`` — advisory
analytics upstream of the PM; no value here ever reaches an order.

The tie-averaged rank helper is a deliberate ~20-line duplicate of
``research/ic.py`` math: live code may never import ``poseidon.research``
(severance invariant, enforced by test_research_isolation.py), and the Python
3.11 floor rules out ``statistics.correlation(method='ranked')`` (3.12+).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

import structlog

from ..core.errors import DataError
from ..core.symbols import is_crypto_symbol
from ..data.base import DataCapability

if TYPE_CHECKING:
    from ..data.router import DataRouter

log = structlog.get_logger(__name__)

CorrelationMethod = Literal["pearson", "spearman"]

_METHODS = ("pearson", "spearman")
# Bounded fan-out for the crypto batch's single-symbol degrade path — mirrors
# the crypto screener's wiring (app.py crypto_screener concurrency).
_CRYPTO_CONCURRENCY = 6


@dataclass
class CorrelationReport:
    """NxN pairwise correlation of daily returns over intersected dates."""

    symbols: list[str]
    method: str
    matrix: list[list[float | None]]  # 1.0 diagonal; None below min_overlap
    pair_observations: list[list[int]]  # aligned return count per pair
    window_start: date | None
    window_end: date | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "method": self.method,
            "matrix": [[round(c, 4) if c is not None else None for c in row]
                       for row in self.matrix],
            "pair_observations": [list(row) for row in self.pair_observations],
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
        }


def _ranks(xs: list[float]) -> list[float]:
    """Tie-averaged 1-based ranks (local duplicate of the research/ic.py math —
    the severance invariant forbids importing it)."""
    order = sorted(range(len(xs)), key=lambda k: xs[k])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation, or None when undefined (n < 2 or a constant leg)."""
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[:n], b[:n]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[k] - mean_a) * (b[k] - mean_b) for k in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a <= 0 or var_b <= 0:
        return None
    return max(-1.0, min(1.0, float(cov / (var_a * var_b) ** 0.5)))


def _spearman(a: list[float], b: list[float]) -> float | None:
    """Spearman rank correlation: pearson over tie-averaged ranks."""
    if min(len(a), len(b)) < 2:
        return None
    return _pearson(_ranks(a), _ranks(b))


def _aligned_returns(sa: dict[date, float], sb: dict[date, float],
                     common: list[date]) -> tuple[list[float], list[float]]:
    """Consecutive-date returns for BOTH legs over the shared calendar, kept in
    lockstep (a span one leg cannot price drops from both)."""
    ra: list[float] = []
    rb: list[float] = []
    for k in range(1, len(common)):
        pa0, pa1 = sa[common[k - 1]], sa[common[k]]
        pb0, pb1 = sb[common[k - 1]], sb[common[k]]
        if pa0 > 0 and pb0 > 0:
            ra.append(pa1 / pa0 - 1.0)
            rb.append(pb1 / pb0 - 1.0)
    return ra, rb


def compute_correlation_matrix(
    closes_by_symbol: dict[str, list[tuple[date, float]]],
    *,
    method: CorrelationMethod,
    min_overlap: int,
) -> CorrelationReport:
    """Pure NxN correlation over (date, close) series.

    Each PAIR is aligned on its intersected dates before returns are taken, so
    mixed 5-day/7-day calendars measure both legs over identical spans. Cells
    below ``min_overlap`` aligned observations — or with a constant leg — are
    ``None``; ``pair_observations`` always reports the measured count.
    """
    if method not in _METHODS:
        raise ValueError(f"unknown correlation method {method!r}; expected one of {_METHODS}")
    symbols = sorted(closes_by_symbol)
    series: dict[str, dict[date, float]] = {
        s: dict(closes_by_symbol[s]) for s in symbols
    }
    n = len(symbols)
    matrix: list[list[float | None]] = [[None] * n for _ in range(n)]
    observations = [[0] * n for _ in range(n)]
    for i, sym in enumerate(symbols):
        matrix[i][i] = 1.0
        observations[i][i] = max(0, len(series[sym]) - 1)
    for i in range(n):
        for j in range(i + 1, n):
            common = sorted(series[symbols[i]].keys() & series[symbols[j]].keys())
            ra, rb = _aligned_returns(series[symbols[i]], series[symbols[j]], common)
            observations[i][j] = observations[j][i] = len(ra)
            if len(ra) < min_overlap:
                continue
            corr = _pearson(ra, rb) if method == "pearson" else _spearman(ra, rb)
            matrix[i][j] = matrix[j][i] = corr
    all_dates = [d for sym in symbols for d in series[sym]]
    return CorrelationReport(
        symbols=symbols, method=method, matrix=matrix,
        pair_observations=observations,
        window_start=min(all_dates) if all_dates else None,
        window_end=max(all_dates) if all_dates else None,
    )


async def gather_correlation_matrix(
    router: DataRouter,
    symbols: list[str],
    *,
    window_days: int,
    method: CorrelationMethod,
    min_overlap: int,
) -> CorrelationReport:
    """Fetch live daily bars for ``symbols`` and compute the matrix.

    Crypto pairs batch through ``bars_multi(require=CRYPTO)`` (a ``/USD`` batch
    must never reach an equity-only provider) with the screener's bounded
    fan-out; everything else uses the plain capable set. Raises
    :class:`DataError` when fewer than two symbols have usable history or no
    pair reaches ``min_overlap`` — an honest gap, never a fabricated matrix.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in symbols:
        sym = raw.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    crypto = [s for s in ordered if is_crypto_symbol(s)]
    equity = [s for s in ordered if not is_crypto_symbol(s)]
    limit = window_days + 1  # window_days returns need window_days + 1 closes
    bars_by_symbol: dict[str, list[Any]] = {}
    if equity:
        bars_by_symbol.update(await router.bars_multi(
            equity, timeframe="1d", limit=limit, require=None))
    if crypto:
        bars_by_symbol.update(await router.bars_multi(
            crypto, timeframe="1d", limit=limit,
            require=DataCapability.CRYPTO, concurrency=_CRYPTO_CONCURRENCY))
    closes_by_symbol: dict[str, list[tuple[date, float]]] = {}
    for sym in ordered:
        pairs = [(b.start.date(), float(b.close)) for b in bars_by_symbol.get(sym, [])]
        if len(pairs) >= 2:  # at least one return
            closes_by_symbol[sym] = pairs
    if len(closes_by_symbol) < 2:
        raise DataError(
            f"correlation needs at least 2 symbols with usable daily history; "
            f"only {len(closes_by_symbol)} of {len(ordered)} requested had any")
    report = compute_correlation_matrix(
        closes_by_symbol, method=method, min_overlap=min_overlap)
    n = len(report.symbols)
    if not any(report.matrix[i][j] is not None
               for i in range(n) for j in range(i + 1, n)):
        raise DataError(
            f"no symbol pair has {min_overlap}+ overlapping daily observations "
            "in the window — correlation unavailable")
    log.info("correlation matrix computed", symbols=len(report.symbols),
             method=method, window_days=window_days)
    return report
