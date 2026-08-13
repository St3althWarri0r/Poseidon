"""Pure behavioral-bias diagnostics: hand-built RoundTrip/Bar fixtures with
exact expected numbers, plus the structural purity pin that keeps
analytics/behavior.py advisory-only (stdlib + core.models + performance)."""
from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import poseidon.analytics.behavior as behavior_module
from poseidon.analytics.behavior import compute_bias_profile
from poseidon.analytics.performance import RoundTrip
from poseidon.core.models import Bar

_BASE = datetime(2026, 6, 1, tzinfo=UTC)  # a Monday: ISO week 23
_NOW = _BASE + timedelta(days=50)


def _dt(day: float) -> datetime:
    return _BASE + timedelta(days=day)


def _trip(symbol: str, entered: float, exited: float, *, entry: str = "100",
          exit_price: str = "110", short: bool = False) -> RoundTrip:
    return RoundTrip(symbol=symbol, strategy="s", quantity=Decimal("1"),
                     entry_price=Decimal(entry), exit_price=Decimal(exit_price),
                     entered_at=_dt(entered), exited_at=_dt(exited), is_short=short)


def _bar(symbol: str, day: float, close: str) -> Bar:
    t = _dt(day)
    return Bar(symbol=symbol, open=Decimal(close), high=Decimal(close),
               low=Decimal(close), close=Decimal(close), volume=1,
               start=t, end=t, source="fake")


def _profile(trips, bars=None, **overrides):
    kwargs = {"window_days": 90, "min_trades": 2, "runup_days": 2,
              "runup_threshold": 0.05, "reentry_days": 3, "now": _NOW}
    kwargs.update(overrides)
    return compute_bias_profile(trips, bars or {}, **kwargs)


def _disposition_trips() -> list[RoundTrip]:
    trips = []
    for i in range(3):  # winners held exactly 2 days
        trips.append(_trip(f"W{i}", 10 + i, 12 + i, exit_price="110"))
    for i in range(3):  # losers held exactly 8 days
        trips.append(_trip(f"L{i}", 10 + i, 18 + i, exit_price="90"))
    return trips


def test_disposition_asymmetry_exact() -> None:
    p = _profile(_disposition_trips(), min_trades=6)
    assert p is not None
    assert p.winner_avg_hold_days == 2.0 and p.loser_avg_hold_days == 8.0
    assert p.hold_asymmetry == 4.0
    assert "disposition" in p.flags


def test_hold_asymmetry_none_when_one_sided() -> None:
    trips = [_trip(f"W{i}", i, i + 2, exit_price="110") for i in range(6)]
    p = _profile(trips, min_trades=6)
    assert p is not None
    assert p.loser_avg_hold_days is None and p.hold_asymmetry is None
    assert "disposition" not in p.flags


def test_zero_pnl_trips_count_neither_side() -> None:
    trips = [_trip(f"Z{i}", i, i + 1, exit_price="100") for i in range(4)]
    trips.append(_trip("W", 10, 12, exit_price="110"))
    p = _profile(trips, min_trades=5)
    assert p is not None
    assert p.winner_avg_hold_days == 2.0  # only the true winner counted
    assert p.loser_avg_hold_days is None


def test_overtrading_iso_week_split_exact() -> None:
    trips = []
    for i in range(5):  # ISO week 23: five losers, PnL -10 each
        trips.append(_trip(f"H{i}", 0.5 + i * 0.1, 1 + i * 0.5, exit_price="90"))
    for day in (8, 15, 22):  # weeks 24/25/26: one +20 winner each
        trips.append(_trip(f"Q{day}", day - 1, day, exit_price="120"))
    p = _profile(trips, min_trades=8)
    assert p is not None
    # median weekly count of [5, 1, 1, 1] is 1: week 23 is high-activity.
    assert p.high_freq_pnl_per_trade == -10.0
    assert p.low_freq_pnl_per_trade == 20.0
    assert "overtrading" in p.flags


def test_freq_split_needs_four_distinct_weeks() -> None:
    trips = []
    for i in range(5):
        trips.append(_trip(f"H{i}", 0.5 + i * 0.1, 1 + i * 0.5, exit_price="90"))
    for day in (8, 15):  # only three distinct weeks in total
        trips.append(_trip(f"Q{day}", day - 1, day, exit_price="120"))
    p = _profile(trips, min_trades=7)
    assert p is not None
    assert p.high_freq_pnl_per_trade is None and p.low_freq_pnl_per_trade is None
    assert "overtrading" not in p.flags


def test_runup_entry_share_directional_with_coverage() -> None:
    bars = {
        "UP": [_bar("UP", 0, "100"), _bar("UP", 1, "103"), _bar("UP", 2, "110")],
        "DN": [_bar("DN", 0, "100"), _bar("DN", 1, "97"), _bar("DN", 2, "89")],
        "FLAT": [_bar("FLAT", 0, "100"), _bar("FLAT", 1, "100"), _bar("FLAT", 2, "101")],
    }
    trips = [
        # Long entered after a +10% two-bar rise: chasing in the long direction.
        _trip("UP", 2.5, 5, exit_price="120"),
        # SHORT entered after an -11% two-bar fall: chasing in the short direction.
        _trip("DN", 2.5, 5, entry="89", exit_price="80", short=True),
        # Covered but no meaningful pre-entry move.
        _trip("FLAT", 2.5, 5),
        # No bars: excluded from the denominator, reported via coverage.
        _trip("NOBARS", 2.5, 5),
    ]
    p = _profile(trips, bars, min_trades=4, reentry_days=1)
    assert p is not None
    assert p.runup_coverage == 0.75
    assert p.runup_entry_share is not None
    assert abs(p.runup_entry_share - 2 / 3) < 1e-9


def test_runup_share_none_without_coverage() -> None:
    p = _profile([_trip("X", 1, 3), _trip("Y", 2, 4)])
    assert p is not None
    assert p.runup_coverage == 0.0 and p.runup_entry_share is None
    assert "chasing" not in p.flags


def test_reentry_share_counts_only_quick_reentries() -> None:
    trips = [
        _trip("RE", 8, 10),
        _trip("RE", 12, 13),   # 2 days after the day-10 exit: counts
        _trip("RE", 25, 26),   # 12 days after the day-13 exit: does not
    ]
    p = _profile(trips, min_trades=3)
    assert p is not None
    assert abs(p.reentry_share - 1 / 3) < 1e-9  # denominator = ALL entries


def test_none_below_min_trades() -> None:
    assert _profile([_trip("A", 1, 3)], min_trades=2) is None


def test_window_filter_excludes_old_trips() -> None:
    old = _trip("OLD", -80, -60)  # exited 110 days before now
    fresh = [_trip(f"F{i}", 10 + i, 12 + i) for i in range(2)]
    p = _profile([old, *fresh], min_trades=2)
    assert p is not None and p.trades == 2


def test_render_bounded_single_printable_line_with_caveat() -> None:
    p = _profile(_disposition_trips(), min_trades=6)
    assert p is not None
    out = p.render(600)
    assert 0 < len(out) <= 600
    assert "\n" not in out and all(c.isprintable() for c in out)
    assert "base-rate" in out and out.endswith("not rules.")
    tiny = p.render(120)  # the caveat survives truncation
    assert len(tiny) <= 120 and tiny.endswith("not rules.")


def test_render_is_deterministic() -> None:
    a = _profile(_disposition_trips(), min_trades=6)
    b = _profile(list(reversed(_disposition_trips())), min_trades=6)
    assert a is not None and b is not None
    assert a.render(600) == b.render(600)


def test_behavior_module_is_pure() -> None:
    """Advisory-only analog of test_research_isolation: behavior.py may import
    nothing beyond the stdlib, core.models, and analytics.performance — no DB,
    router, LLM, risk, or execution surface can ever creep in."""
    src = Path(behavior_module.__file__).read_text()
    package = "poseidon.analytics"
    allowed_project = {"poseidon.core.models", "poseidon.analytics.performance"}
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib, \
                    f"non-stdlib import in behavior.py: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                mod = node.module or ""
                if mod.split(".")[0] in stdlib:
                    continue
                assert mod in allowed_project, f"forbidden import in behavior.py: {mod}"
            else:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)])
                mod = f"{base}.{node.module}" if node.module else base
                assert mod in allowed_project, \
                    f"forbidden relative import in behavior.py: {mod}"
