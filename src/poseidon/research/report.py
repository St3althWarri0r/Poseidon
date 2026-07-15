"""Run a factor set over a universe and rank by |t-stat|. Descriptive point-in-time
IC over the supplied history — NOT tradable PnL, and noisy on a thin universe."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Bar
from .factors import Factor
from .ic import ICResult, evaluate_factor

_THIN_UNIVERSE = 20     # cross-sectional IC below this many names is very noisy


@dataclass(frozen=True)
class FactorReport:
    results: list[ICResult]
    cross_section_size: int
    thin: bool

    def render(self) -> str:
        head = (f"Factor IC/IR report — universe {self.cross_section_size} symbols"
                + ("  [THIN: results are noisy/unreliable]" if self.thin else ""))
        cols = f"{'factor':<20} {'IC':>8} {'IR':>8} {'t-stat':>8} {'hit':>6}  decay"
        rows = []
        for r in self.results:
            decay = " ".join(f"{h}:{v:+.3f}" for h, v in sorted(r.ic_by_horizon.items()))
            rows.append(f"{r.factor:<20} {r.ic_mean:>+8.4f} {r.ir:>+8.3f} "
                        f"{r.t_stat:>+8.2f} {r.hit_rate:>6.2f}  {decay}")
        note = ("Descriptive point-in-time IC; not tradable PnL (no costs/capacity); "
                "mining many factors on one universe overfits.")
        return "\n".join([head, "", cols, *rows, "", note])


def run_report(factors: list[Factor], history: dict[str, list[Bar]], *, horizon: int,
               rebalance_every: int, horizons: list[int], min_cross: int = 5) -> FactorReport:
    results = [evaluate_factor(f, history, horizon=horizon, rebalance_every=rebalance_every,
                               horizons=horizons, min_cross=min_cross) for f in factors]
    results.sort(key=lambda r: abs(r.t_stat), reverse=True)
    size = len(history)
    return FactorReport(results=results, cross_section_size=size, thin=size < _THIN_UNIVERSE)
