"""Run a factor set over a universe and rank by |t-stat|. Descriptive point-in-time
IC over the supplied history — NOT tradable PnL, and noisy on a thin universe. The
thin flag keys on the EFFECTIVE cross-section (names surviving min_bars/None gating
per sample, surfaced per row as `names`), not on how many symbols merely had bars.

Rigor additions (design §4.8): the main table carries the random-control null columns
(alphaIC / alpha_t) and an honest verdict; an optional OOS-split block, a quantile
group-equity section, and labeled footer notes (alpha_IC definition, gate + Harvey-
Liu-Zhu multiple-testing caveat, and — only with a bundled universe — a survivorship
caveat) follow. Every never-computed value renders as `-`, never a fake 0.0."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..core.models import Bar
from .factors import Factor
from .groups import GroupEquityResult, compute_group_equity
from .ic import ICResult, NullSpec, evaluate_factor

_DEFAULT_NULL = NullSpec()

_THIN_UNIVERSE = 20     # cross-sectional IC below this many names is very noisy


def _opt_num(x: float | None, spec: str, width: int) -> str:
    """Render an optional number: `format(x, spec)` when present, else a right-aligned
    `-` in `width` columns — the finding-14 discipline (never-computed != measured 0.0)."""
    return format(x, spec) if x is not None else f"{'-':>{width}}"


@dataclass(frozen=True)
class FactorReport:
    results: list[ICResult]
    cross_section_size: int
    thin: bool
    # Rigor render context (design §4.7/§4.8). `groups` is aligned 1:1 with `results`
    # (rank order). `split_ran` is whether the OOS split was requested (train_frac > 0)
    # — the block then renders per factor, with `-` for any factor whose split was n/a.
    # `embargo` = stride-1 test samples dropped, shown in the block header (not in §4.7's
    # field list, but render() has no other access to the stride — noted deviation).
    groups: list[GroupEquityResult] = field(default_factory=list)
    null: NullSpec = _DEFAULT_NULL
    split_ran: bool = False
    embargo: int = 0
    universe_note: str = ""

    def render(self) -> str:
        head = (f"Factor IC/IR report — universe {self.cross_section_size} symbols"
                + ("  [THIN: results are noisy/unreliable]" if self.thin else ""))
        cols = (f"{'factor':<20} {'IC':>8} {'IR':>8} {'t-stat':>8} {'hit':>6} "
                f"{'n':>4} {'names':>6} {'alphaIC':>9} {'alpha_t':>8}  "
                f"{'verdict':<15} decay")
        rows = []
        for r in self.results:
            if r.n_periods == 0:
                rows.append(f"{r.factor:<20} no data (insufficient history for "
                            f"min_bars/horizon)")
                continue
            decay = " ".join(f"{h}:{v:+.3f}" if v is not None else f"{h}:-"
                             for h, v in sorted(r.ic_by_horizon.items()))
            alpha_mean = _opt_num(r.alpha_mean, ">+9.4f", 9)
            alpha_t = _opt_num(r.alpha_t, ">+8.2f", 8)
            rows.append(f"{r.factor:<20} {r.ic_mean:>+8.4f} {r.ir:>+8.3f} "
                        f"{r.t_stat:>+8.2f} {r.hit_rate:>6.2f} {r.n_periods:>4} "
                        f"{r.breadth:>6} {alpha_mean} {alpha_t}  {r.verdict:<15} {decay}")

        blocks: list[str] = [head, "", cols, *rows, ""]

        if self.split_ran:
            blocks.append(f"OOS split (train_frac={self.null.train_frac:.2f}, "
                          f"embargo={self.embargo} samples):")
            for r in self.results:
                if r.n_periods == 0:
                    continue
                tr = _opt_num(r.alpha_t_train, ">+8.2f", 8)
                te = _opt_num(r.alpha_t_test, ">+8.2f", 8)
                blocks.append(f"  {r.factor:<20} train {tr}  test {te}")
            blocks.append("")

        n_groups = self.groups[0].n_groups if self.groups else 0
        blocks.append(f"Quantile layering (n_groups={n_groups}, hold = rebalance interval)")
        for g in self.groups:
            blocks.append(f"  {g.factor:<20} {_group_line(g)}")
        blocks.append("")

        thr = self.null.alpha_t_threshold
        notes = [
            "alpha_IC = per-date IC minus mean of N seeded within-date score "
            "permutations; alpha_t uses the same non-overlapping n_eff as the base t-stat.",
            f"Gate: ic_mean > 0.02, hit >= 0.55, |t| > 2, alpha_t >= {thr:.1f} "
            "(single-test). Harvey-Liu-Zhu (2016): factors found by scanning many "
            "candidates should clear |t| >= 3.5 — treat confirmed_alive below that as "
            "provisional when ranking the whole library.",
        ]
        if self.universe_note:
            notes.append(
                f"UNIVERSE CAVEAT [{self.universe_note}]: current-membership snapshot "
                "(as_of 2026-07) — survivorship bias: delisted members are absent; IC "
                "over today's survivors overstates efficacy.")
        notes.append(
            "Descriptive point-in-time IC; not tradable PnL (no costs/capacity); "
            "mining many factors on one universe overfits.")
        return "\n".join([*blocks, *notes])


def _group_line(g: GroupEquityResult) -> str:
    """One factor's quantile readout, or the insufficient-data reason (design §4.8)."""
    if g.insufficient:
        return f"insufficient data ({g.reason})"
    buckets = " ".join(f"G{i + 1} {tr * 100:+.1f}%"
                       for i, tr in enumerate(g.total_return))
    ls = f"{g.long_short * 100:+.1f}%" if g.long_short is not None else "-"
    rho = f"{g.mono_rho:+.2f}" if g.mono_rho is not None else "-"
    return f"{buckets} | L/S {ls} | mono rho {rho}"


def run_report(factors: list[Factor], history: dict[str, list[Bar]], *, horizon: int,
               rebalance_every: int, horizons: list[int], min_cross: int = 5,
               null: NullSpec = _DEFAULT_NULL, n_groups: int = 5,
               universe_note: str = "") -> FactorReport:
    results = [evaluate_factor(f, history, horizon=horizon, rebalance_every=rebalance_every,
                               horizons=horizons, min_cross=min_cross, null=null)
               for f in factors]
    results.sort(key=lambda r: abs(r.t_stat), reverse=True)
    # Quantile layering, aligned 1:1 with the ranked results (same factor order).
    by_name = {f.name: f for f in factors}
    groups = [compute_group_equity(by_name[r.factor], history, n_groups=n_groups,
                                   rebalance_every=rebalance_every, min_cross=min_cross)
              for r in results]
    size = len(history)
    breadths = [r.breadth for r in results if r.n_periods > 0]
    effective = min(breadths) if breadths else 0
    # The OOS block renders whenever a split was requested; the embargo is the base
    # series' non-overlap stride minus one (design §4.2).
    split_ran = null.train_frac > 0.0
    embargo = max(1, math.ceil(horizon / max(1, rebalance_every))) - 1
    return FactorReport(results=results, cross_section_size=size,
                        thin=size < _THIN_UNIVERSE or effective < _THIN_UNIVERSE,
                        groups=groups, null=null, split_ran=split_ran, embargo=embargo,
                        universe_note=universe_note)
