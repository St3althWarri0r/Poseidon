"""Async scheduler.

Jobs are registered by name; schedules from configuration bind triggers
(fixed interval from every second upward, or standard 5-field cron
evaluated in America/New_York) to jobs. Overlap protection: a job still
running when its next trigger fires is skipped for that tick, never
double-run. Job exceptions are logged and reported — the scheduler itself
never dies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter

from ..core.clock import MarketClock
from ..core.config import ScheduleConfig
from ..core.enums import MarketSession
from ..core.errors import ConfigError
from ..core.events import EventBus, Topics

log = structlog.get_logger(__name__)

Job = Callable[[], Awaitable[None]]
EASTERN = ZoneInfo("America/New_York")


class Scheduler:
    def __init__(self, clock: MarketClock, bus: EventBus) -> None:
        self._clock = clock
        self._bus = bus
        self._jobs: dict[str, Job] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._running_jobs: set[str] = set()
        self._stop = asyncio.Event()
        self.last_runs: dict[str, str] = {}

    def register_job(self, name: str, job: Job) -> None:
        if name in self._jobs:
            raise ConfigError(f"job '{name}' already registered")
        self._jobs[name] = job

    def start(self, schedules: list[ScheduleConfig]) -> None:
        for schedule in schedules:
            if not schedule.enabled:
                continue
            if schedule.job not in self._jobs:
                raise ConfigError(
                    f"schedule '{schedule.name}' references unknown job '{schedule.job}'. "
                    f"Registered: {', '.join(sorted(self._jobs))}"
                )
            if schedule.cron and not croniter.is_valid(schedule.cron):
                raise ConfigError(f"schedule '{schedule.name}': invalid cron '{schedule.cron}'")
            task = asyncio.create_task(self._run_schedule(schedule), name=f"sched-{schedule.name}")
            self._tasks.append(task)
        log.info("scheduler started", schedules=len(self._tasks))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def trigger_now(self, job_name: str) -> None:
        """Manual trigger from the CLI/dashboard."""
        if job_name not in self._jobs:
            raise KeyError(f"unknown job '{job_name}'")
        await self._execute(job_name, f"manual:{job_name}")

    async def _run_schedule(self, schedule: ScheduleConfig) -> None:
        while not self._stop.is_set():
            delay = self._next_delay(schedule)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # stopping
            except TimeoutError:
                pass
            if schedule.only_market_hours and self._clock.session() is not MarketSession.REGULAR:
                continue
            await self._execute(schedule.job, schedule.name)

    def _next_delay(self, schedule: ScheduleConfig) -> float:
        if schedule.every_seconds is not None:
            return float(schedule.every_seconds)
        assert schedule.cron is not None
        now = datetime.now(EASTERN)
        next_fire = croniter(schedule.cron, now).get_next(datetime)
        return max((next_fire - now).total_seconds(), 0.5)

    async def _execute(self, job_name: str, schedule_name: str) -> None:
        if job_name in self._running_jobs:
            log.debug("job still running; skipping tick", job=job_name, schedule=schedule_name)
            return
        self._running_jobs.add(job_name)
        started = datetime.now(EASTERN)
        try:
            await self._jobs[job_name]()
            self.last_runs[job_name] = started.isoformat()
        except Exception as exc:
            log.exception("scheduled job failed", job=job_name, schedule=schedule_name)
            await self._bus.publish(Topics.SYSTEM_ERROR,
                                    {"component": f"job:{job_name}", "error": str(exc)})
        finally:
            self._running_jobs.discard(job_name)
