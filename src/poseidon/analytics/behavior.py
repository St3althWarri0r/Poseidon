"""Behavioral bias diagnostics over the platform's own closed round trips.

ADVISORY ONLY: pure, deterministic functions with zero side effects — no LLM,
no DB, no router, and never an import of the risk engine, the order manager,
or the execution layer (pinned structurally by tests/unit/test_behavior.py).
The resulting profile is rendered to a single advisory prose line for the PM's
lesson block; it never tunes risk parameters, sizing, cooldowns, or cadence.

Four diagnostics, each a classic retail failure mode:

* hold-time asymmetry — losers held far longer than winners (disposition);
* trade frequency vs marginal PnL — busy weeks that pay worse than quiet ones
  (overtrading);
* entry-after-runup share — entries chasing a strong pre-entry move in the
  entry's own direction (longs after a rise, shorts after a fall);
* re-entry proximity — re-entering a symbol within days of exiting it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from ..core.models import Bar
from .performance import RoundTrip

# Flag thresholds are module constants, not config: they label advisory
# numbers that already appear alongside them — tuning knobs would imply a
# precision these small-sample heuristics do not have.
_DISPOSITION_RATIO = 2.0
_DISPOSITION_MIN_PER_SIDE = 3
_CHASING_SHARE = 0.4
_CHASING_MIN_COVERED = 3
_REENTRY_SHARE_FLAG = 0.25
_FREQ_MIN_WEEKS = 4

_CAVEAT = ("Small sample of your own trades - treat these as base-rate "
           "tendencies to weigh, not rules.")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass
class BiasProfile:
    """Deterministic bias diagnostics for one account's recent closed trades.

    ``None`` metrics mean "not enough evidence" (empty side, too few distinct
    weeks, or zero bar coverage) — never zero, so a silent absence can't read
    as a clean bill of health.
    """

    window_days: int
    trades: int
    winner_avg_hold_days: float | None
    loser_avg_hold_days: float | None
    hold_asymmetry: float | None  # loser/winner hold; None if either side empty
    high_freq_pnl_per_trade: float | None
    low_freq_pnl_per_trade: float | None
    runup_days: int
    runup_threshold: float
    runup_entry_share: float | None  # share of COVERED entries after a runup
    runup_coverage: float  # share of entries with bar coverage
    reentry_days: int
    reentry_share: float  # denominator: ALL entries in the window
    flags: list[str] = field(default_factory=list)

    def render(self, max_chars: int = 600) -> str:
        """Deterministic single-printable-line summary, always ending with the
        small-sample caveat (which survives truncation)."""
        parts = [f"Diagnostics from your last {self.trades} closed trades "
                 f"({self.window_days}d window)."]
        if self.hold_asymmetry is not None:
            parts.append(
                f"Winners were held {self.winner_avg_hold_days:.1f}d on average vs "
                f"{self.loser_avg_hold_days:.1f}d for losers "
                f"({self.hold_asymmetry:.1f}x asymmetry).")
        if self.high_freq_pnl_per_trade is not None and self.low_freq_pnl_per_trade is not None:
            parts.append(
                f"High-activity weeks averaged {self.high_freq_pnl_per_trade:+.2f} "
                f"PnL/trade vs {self.low_freq_pnl_per_trade:+.2f} in quieter weeks.")
        if self.runup_entry_share is not None:
            parts.append(
                f"{self.runup_entry_share:.0%} of bar-covered entries followed a "
                f"{self.runup_days}-bar move beyond {self.runup_threshold:.0%} in the "
                f"entry's direction (coverage {self.runup_coverage:.0%}).")
        parts.append(
            f"{self.reentry_share:.0%} of entries came within {self.reentry_days}d "
            f"of exiting the same symbol.")
        if self.flags:
            parts.append("Tendencies flagged: " + ", ".join(self.flags) + ".")
        body = "".join(c for c in " ".join(" ".join(parts).split()) if c.isprintable())
        budget = max_chars - len(_CAVEAT) - 1
        if len(body) > budget:
            body = body[:max(budget, 0)].rstrip()
        return (body + " " + _CAVEAT).strip()[:max_chars]


def _entry_after_runup(trip: RoundTrip, bars: list[Bar], runup_days: int,
                       threshold: float) -> bool | None:
    """Did the ``runup_days`` close-to-close move immediately before this entry
    exceed ``threshold`` in the entry's own direction? None = no bar coverage
    (missing bars, not enough history, or a non-positive base close)."""
    idx: int | None = None
    for i, b in enumerate(bars):  # bars are end-sorted by the caller
        if b.end <= trip.entered_at:
            idx = i
        else:
            break
    if idx is None or idx - runup_days < 0:
        return None
    p1, p0 = float(bars[idx].close), float(bars[idx - runup_days].close)
    if p0 <= 0:
        return None
    move = p1 / p0 - 1
    return (move < -threshold) if trip.is_short else (move > threshold)


def compute_bias_profile(trips: list[RoundTrip], bars_by_symbol: dict[str, list[Bar]],
                         *, window_days: int, min_trades: int, runup_days: int,
                         runup_threshold: float, reentry_days: int,
                         now: datetime) -> BiasProfile | None:
    """Pure diagnostics over closed round trips (None below ``min_trades`` —
    the sample-size guard). ``bars_by_symbol`` is best-effort: symbols without
    bars only lose run-up coverage, never the whole profile."""
    cutoff = now - timedelta(days=window_days)
    window = [t for t in trips if t.exited_at >= cutoff]
    if len(window) < min_trades:
        return None

    # Disposition: winners are pnl > 0, losers pnl < 0; zero-pnl excluded.
    winners = [t for t in window if t.pnl > 0]
    losers = [t for t in window if t.pnl < 0]
    winner_avg = _mean([t.holding_days for t in winners])
    loser_avg = _mean([t.holding_days for t in losers])
    asymmetry: float | None = None
    if winner_avg is not None and loser_avg is not None and winner_avg > 0:
        asymmetry = loser_avg / winner_avg

    # Overtrading: above-median vs at/below-median ISO-week trade-count weeks.
    weeks: dict[tuple[int, int], list[RoundTrip]] = defaultdict(list)
    for t in window:
        iso = t.exited_at.isocalendar()
        weeks[(iso[0], iso[1])].append(t)
    high_freq: float | None = None
    low_freq: float | None = None
    if len(weeks) >= _FREQ_MIN_WEEKS:
        med = median([len(v) for v in weeks.values()])
        high_freq = _mean([float(t.pnl) for v in weeks.values() if len(v) > med for t in v])
        low_freq = _mean([float(t.pnl) for v in weeks.values() if len(v) <= med for t in v])

    # Entry-after-runup: longs after a rise, shorts after a fall.
    sorted_bars = {sym: sorted(b, key=lambda x: x.end) for sym, b in bars_by_symbol.items()}
    covered = after_runup = 0
    for t in window:
        state = _entry_after_runup(t, sorted_bars.get(t.symbol, []),
                                   runup_days, runup_threshold)
        if state is None:
            continue
        covered += 1
        after_runup += int(state)
    coverage = covered / len(window)
    runup_share = (after_runup / covered) if covered else None

    # Re-entry proximity: an entry within reentry_days of ANY prior same-symbol
    # exit. A trip's own exit can never match (exits follow their entry).
    exits: dict[str, list[datetime]] = defaultdict(list)
    for t in window:
        exits[t.symbol].append(t.exited_at)
    max_gap = timedelta(days=reentry_days)
    reentries = sum(
        1 for t in window
        if any(timedelta(0) <= t.entered_at - e <= max_gap for e in exits[t.symbol]))
    reentry_share = reentries / len(window)

    flags: list[str] = []
    if (asymmetry is not None and asymmetry >= _DISPOSITION_RATIO
            and len(winners) >= _DISPOSITION_MIN_PER_SIDE
            and len(losers) >= _DISPOSITION_MIN_PER_SIDE):
        flags.append("disposition")
    if (high_freq is not None and low_freq is not None
            and high_freq < 0 <= low_freq):
        flags.append("overtrading")
    if (runup_share is not None and runup_share >= _CHASING_SHARE
            and covered >= _CHASING_MIN_COVERED):
        flags.append("chasing")
    if reentry_share >= _REENTRY_SHARE_FLAG:
        flags.append("rapid_reentry")

    return BiasProfile(
        window_days=window_days, trades=len(window),
        winner_avg_hold_days=winner_avg, loser_avg_hold_days=loser_avg,
        hold_asymmetry=asymmetry,
        high_freq_pnl_per_trade=high_freq, low_freq_pnl_per_trade=low_freq,
        runup_days=runup_days, runup_threshold=runup_threshold,
        runup_entry_share=runup_share, runup_coverage=coverage,
        reentry_days=reentry_days, reentry_share=reentry_share, flags=flags)
