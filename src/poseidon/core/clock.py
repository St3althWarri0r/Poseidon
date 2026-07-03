"""Market clock and data-freshness policy.

Two safety-critical jobs live here:

1. Knowing whether US equity markets are open (regular / extended / closed),
   including weekends, full holidays, and half days, so the scheduler and risk
   engine can gate trading windows.
2. Enforcing the "live data only" contract: every timestamped datum is graded
   REAL_TIME / DELAYED / STALE, and STALE data is rejected upstream of the AI.

The holiday table is shipped for the current and next calendar year and is
validated at startup; if the table does not cover 'today', the market is
treated as CLOSED (fail-safe) and a critical notification is raised by the
watchdog rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .enums import DataFreshness, MarketSession

EASTERN = ZoneInfo("America/New_York")

# NYSE full-day holidays. Source: NYSE published calendar. Kept two years deep;
# the watchdog raises a config alert when coverage drops below ~60 days.
FULL_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }
)

# Early closes (13:00 ET).
HALF_DAYS: frozenset[date] = frozenset(
    {
        date(2026, 11, 27), date(2026, 12, 24),
        date(2027, 11, 26),
    }
)

_CALENDAR_YEARS: frozenset[int] = frozenset(d.year for d in FULL_HOLIDAYS)

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)
PRE_MARKET_OPEN = time(4, 0)
AFTER_HOURS_CLOSE = time(20, 0)


def utc_now() -> datetime:
    return datetime.now(UTC)


def calendar_covers(day: date) -> bool:
    return day.year in _CALENDAR_YEARS


@dataclass(frozen=True)
class FreshnessPolicy:
    """Thresholds (seconds) for grading data age. Configurable per deployment."""

    real_time_max_age: float = 5.0
    delayed_max_age: float = 900.0  # 15 minutes — typical delayed-feed window

    def grade(self, as_of: datetime, *, now: datetime | None = None) -> DataFreshness:
        now = now or utc_now()
        if as_of.tzinfo is None:
            # Naive timestamps are untrustworthy: treat as stale, never assume.
            return DataFreshness.STALE
        age = (now - as_of).total_seconds()
        if age < 0:
            # Clock skew from a provider; small negative ages are tolerated.
            return DataFreshness.REAL_TIME if age > -5 else DataFreshness.STALE
        if age <= self.real_time_max_age:
            return DataFreshness.REAL_TIME
        if age <= self.delayed_max_age:
            return DataFreshness.DELAYED
        return DataFreshness.STALE


class MarketClock:
    def __init__(self, *, tz: ZoneInfo = EASTERN) -> None:
        self._tz = tz

    def now_eastern(self) -> datetime:
        return datetime.now(self._tz)

    def session(self, at: datetime | None = None) -> MarketSession:
        moment = (at or utc_now()).astimezone(self._tz)
        day = moment.date()
        if not calendar_covers(day):
            return MarketSession.CLOSED  # fail-safe: unknown calendar year
        if moment.weekday() >= 5 or day in FULL_HOLIDAYS:
            return MarketSession.CLOSED
        close = HALF_DAY_CLOSE if day in HALF_DAYS else REGULAR_CLOSE
        t = moment.time()
        if REGULAR_OPEN <= t < close:
            return MarketSession.REGULAR
        if PRE_MARKET_OPEN <= t < REGULAR_OPEN:
            return MarketSession.PRE_MARKET
        if close <= t < AFTER_HOURS_CLOSE and day not in HALF_DAYS:
            return MarketSession.AFTER_HOURS
        return MarketSession.CLOSED

    def is_trading_day(self, day: date | None = None) -> bool:
        day = day or self.now_eastern().date()
        return calendar_covers(day) and day.weekday() < 5 and day not in FULL_HOLIDAYS

    def next_open(self, at: datetime | None = None) -> datetime:
        """Next regular-session open, in UTC."""
        moment = (at or utc_now()).astimezone(self._tz)
        candidate = moment.date()
        for _ in range(30):
            open_dt = datetime.combine(candidate, REGULAR_OPEN, tzinfo=self._tz)
            if self.is_trading_day(candidate) and open_dt > moment:
                return open_dt.astimezone(UTC)
            candidate += timedelta(days=1)
        raise RuntimeError("no trading day found within 30 days — check holiday calendar")
