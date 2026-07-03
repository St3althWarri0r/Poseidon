"""Application kernel.

Constructs and wires every subsystem in dependency order, owns the main
review-cycle job, and supervises shutdown. This is the composition root —
all construction happens here; subsystems never reach for globals.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from datetime import UTC, datetime

import structlog

from . import __version__
from .ai.agent import ClaudeAgent
from .ai.reports import render_decision_report
from .ai.tools import ToolDispatcher
from .api.server import DashboardServer
from .brokers.base import Broker
from .brokers.plugins.paper import PaperBroker
from .brokers.registry import create_broker
from .core.clock import FreshnessPolicy, MarketClock, calendar_covers
from .core.config import AppConfig
from .core.container import Container
from .core.enums import HealthState, TradingMode
from .core.errors import AgentError, AgentRefusedError, ConfigError, DataError
from .core.events import EventBus, Topics
from .data.providers import BUILTIN_PROVIDERS
from .data.router import DataRouter
from .execution.approvals import ApprovalQueue
from .execution.manager import OrderManager
from .health.monitor import HealthMonitor
from .notifications.service import NotificationService
from .portfolio.state import PortfolioState
from .portfolio.sync import PortfolioSyncService
from .risk.engine import RiskEngine
from .scheduler.scheduler import Scheduler
from .security.audit import AuditLog
from .security.vault import Vault
from .storage.db import Database
from .strategy.engine import StrategyEngine
from .updater import UpdateService

log = structlog.get_logger(__name__)


class ApplicationKernel:
    def __init__(self, config: AppConfig, vault: Vault) -> None:
        self.config = config
        self.vault = vault
        self.container = Container()
        self.bus = EventBus()
        self.clock = MarketClock()
        self.portfolio = PortfolioState()

        # populated in start()
        self.db: Database
        self.audit: AuditLog
        self.router: DataRouter
        self.broker: Broker
        self.risk: RiskEngine
        self.approvals: ApprovalQueue
        self.order_manager: OrderManager
        self.agent: ClaudeAgent | None = None
        self.strategies: StrategyEngine
        self.scheduler: Scheduler
        self.health: HealthMonitor
        self.notifier: NotificationService
        self.sync: PortfolioSyncService
        self.dashboard: DashboardServer
        self.updates: UpdateService
        self._cycle_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------ wiring

    async def start(self) -> None:
        cfg = self.config
        log.info("starting Aegis Trader", version=__version__, mode=cfg.mode)

        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(cfg.data_dir / "aegis.db")
        await self.db.open()
        self.audit = AuditLog(self.db)
        ok, bad_seq = await self.audit.verify_chain()
        if not ok:
            raise ConfigError(
                f"audit chain verification FAILED at seq {bad_seq} — the audit log has been "
                "tampered with or corrupted; refusing to start (see docs/troubleshooting.md)"
            )

        self.router = self._build_router()
        self.broker = await self._build_broker()
        self.risk = RiskEngine(cfg.risk, self.portfolio, self.router, self.clock, self.bus)
        self.approvals = ApprovalQueue(self.bus)
        self.order_manager = OrderManager(
            self.broker, self.risk, self.approvals, self.db, self.audit, self.bus, mode=cfg.mode
        )
        dispatcher = ToolDispatcher(
            self.router, self.portfolio, self.risk,
            allow_delayed_quotes=cfg.data.allow_delayed_for_research,
        )
        api_key = self.vault.get(cfg.ai.api_key_credential)
        self.agent = ClaudeAgent(cfg.ai, api_key, dispatcher)

        self.strategies = StrategyEngine(cfg.strategies, cfg.all_watchlist_symbols())
        self.notifier = NotificationService(cfg.notifications, self.vault, self.bus)
        self.sync = PortfolioSyncService(self.broker, self.portfolio, self.bus, self.db, self.clock)
        self.scheduler = Scheduler(self.clock, self.bus)
        self._register_jobs()
        self.health = HealthMonitor(self.bus)
        self._register_probes()
        self.updates = UpdateService(cfg.updates, self.bus)
        self.dashboard = DashboardServer(self, host=cfg.dashboard.host, port=cfg.dashboard.port)

        await self.audit.append("system", "startup", {"version": __version__, "mode": cfg.mode.value})
        await self.sync.start()
        await self.order_manager.resume_open_orders()
        self.scheduler.start(self._effective_schedules())
        await self.health.start()
        await self.updates.start()
        await self.dashboard.start()
        log.info("Aegis Trader is up",
                 dashboard=f"http://{cfg.dashboard.host}:{cfg.dashboard.port}",
                 broker=self.broker.name, mode=cfg.mode.value)

    def _build_router(self) -> DataRouter:
        providers = []
        for provider_cfg in self.config.data.providers:
            if not provider_cfg.enabled:
                continue
            cls = BUILTIN_PROVIDERS.get(provider_cfg.name)
            if cls is None:
                raise ConfigError(
                    f"unknown data provider '{provider_cfg.name}'. "
                    f"Available: {', '.join(sorted(BUILTIN_PROVIDERS))}"
                )
            options = dict(provider_cfg.options)
            credential = self.vault.get(provider_cfg.credential) if provider_cfg.credential else ""
            api_key = credential
            # Providers with multi-field credentials store JSON in the vault.
            if credential.strip().startswith("{"):
                blob = json.loads(credential)
                api_key = blob.pop("key_id", blob.pop("api_key", ""))
                options.update(blob)
            providers.append(
                (cls(api_key=api_key, timeout=self.config.data.request_timeout_seconds,
                     options=options), provider_cfg.priority)
            )
        if not providers:
            raise ConfigError("no market data providers configured — Aegis cannot run without live data")
        return DataRouter(
            providers,
            FreshnessPolicy(
                real_time_max_age=self.config.data.real_time_max_age_seconds,
                delayed_max_age=self.config.data.delayed_max_age_seconds,
            ),
        )

    async def _build_broker(self) -> Broker:
        broker_cfg = self.config.primary_broker()
        if broker_cfg is None:
            # Research mode without a broker: use the paper broker so the
            # portfolio surface still works.
            broker_cfg_options: dict[str, object] = {
                "state_file": str(self.config.data_dir / "paper_state.json")
            }
            broker: Broker = PaperBroker(credentials={}, options=broker_cfg_options)
        else:
            credentials: dict[str, str] = {}
            if broker_cfg.credential:
                credentials = self.vault.get_json(broker_cfg.credential)
            options = dict(broker_cfg.options)
            if broker_cfg.name == "paper":
                options.setdefault("state_file", str(self.config.data_dir / "paper_state.json"))
            broker = create_broker(broker_cfg.name, credentials=credentials,
                                   paper=broker_cfg.paper, options=options)
        if isinstance(broker, PaperBroker):
            broker.set_quote_fn(lambda symbol: self.router.quote(symbol, allow_delayed=True))
        await broker.connect()
        return broker

    def _register_jobs(self) -> None:
        self.scheduler.register_job("review_cycle", self.run_review_cycle)
        self.scheduler.register_job("portfolio_sync", self.sync.sync_once)
        self.scheduler.register_job("update_check", self._update_check_job)
        self.scheduler.register_job("audit_verify", self._audit_verify_job)

    def _effective_schedules(self):
        """Config schedules plus a default review cadence if none is defined."""
        from .core.config import ScheduleConfig

        schedules = list(self.config.schedules)
        if not any(s.job == "review_cycle" and s.enabled for s in schedules):
            schedules.append(
                ScheduleConfig(name="default-review", job="review_cycle",
                               every_seconds=self.config.ai.review_interval_seconds,
                               only_market_hours=True)
            )
        if not any(s.job == "audit_verify" and s.enabled for s in schedules):
            schedules.append(
                ScheduleConfig(name="nightly-audit-verify", job="audit_verify",
                               cron="15 2 * * *")
            )
        return schedules

    def _register_probes(self) -> None:
        async def broker_probe() -> tuple[HealthState, str | None]:
            ok = await self.broker.ping()
            return (HealthState.HEALTHY, None) if ok else (HealthState.UNHEALTHY, "ping failed")

        async def data_probe() -> tuple[HealthState, str | None]:
            statuses = self.router.provider_status()
            down = [s["name"] for s in statuses if not s["available"]]
            if not down:
                return HealthState.HEALTHY, None
            if len(down) < len(statuses):
                return HealthState.DEGRADED, f"penalized: {', '.join(map(str, down))}"
            return HealthState.UNHEALTHY, "all providers penalized"

        async def sync_probe() -> tuple[HealthState, str | None]:
            age = self.portfolio.age_seconds
            if age is None:
                return HealthState.DEGRADED, "never synced"
            if age > 300:
                return HealthState.UNHEALTHY, f"stale by {age:.0f}s"
            return HealthState.HEALTHY, None

        async def calendar_probe() -> tuple[HealthState, str | None]:
            today = self.clock.now_eastern().date()
            if not calendar_covers(today):
                return HealthState.UNHEALTHY, "holiday calendar does not cover today — update Aegis"
            return HealthState.HEALTHY, None

        self.health.register("broker", broker_probe)
        self.health.register("market_data", data_probe)
        self.health.register("portfolio_sync", sync_probe)
        self.health.register("holiday_calendar", calendar_probe)

    # ------------------------------------------------------------- main cycle

    async def run_review_cycle(self) -> None:
        """One full AI review cycle: scan strategies, run the agent, persist
        the decision, execute through the order manager."""
        if self.agent is None:
            return
        if self._cycle_lock.locked():
            log.info("review cycle already running; skipping")
            return
        async with self._cycle_lock:
            started = datetime.now(UTC)
            try:
                signals = await self.strategies.scan_all(self.router, self.portfolio)
                decision = await self.agent.run_cycle(
                    mode=self.order_manager.mode,
                    watchlist=self.config.all_watchlist_symbols(),
                    enabled_strategies=self.strategies.enabled_names,
                    strategy_signals=[s.as_dict() for s in signals],
                    market_session=self.clock.session().value,
                )
            except AgentRefusedError as exc:
                log.warning("agent refused; cycle skipped", error=str(exc))
                return
            except (AgentError, DataError) as exc:
                log.error("review cycle failed", error=str(exc))
                await self.bus.publish(Topics.SYSTEM_ERROR,
                                       {"component": "review_cycle", "error": str(exc)})
                return

            await self.db.execute(
                "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (decision.id, decision.cycle_id, decision.action.value,
                 json.dumps(decision.model_dump(mode="json")),
                 (decision.created_at or started).isoformat()),
            )
            await self.audit.append("ai", "decision", {
                "decision_id": decision.id, "action": decision.action.value,
                "trades": len(decision.trades), "sources": decision.data_sources,
            })
            await self.bus.publish(Topics.DECISION_MADE, decision.model_dump(mode="json"))

            if decision.trades:
                report = render_decision_report(decision)
                await self.bus.publish(Topics.NOTIFY, {
                    "level": "info",
                    "title": f"AI decision: {decision.action.value} ({len(decision.trades)} trade(s))",
                    "body": report[:3500],
                })
                await self.order_manager.execute_decision(decision)
            log.info("review cycle complete", cycle=decision.cycle_id,
                     action=decision.action.value, trades=len(decision.trades),
                     duration_s=round((datetime.now(UTC) - started).total_seconds(), 1))

    async def _update_check_job(self) -> None:
        await self.updates.check_once()

    async def _audit_verify_job(self) -> None:
        ok, bad_seq = await self.audit.verify_chain()
        if not ok:
            self.risk.circuit.force_open(f"audit chain corrupt at seq {bad_seq}")
            await self.bus.publish(Topics.NOTIFY, {
                "level": "critical", "title": "Audit chain verification failed",
                "body": f"Record {bad_seq} does not verify. Trading halted.",
            })

    # ---------------------------------------------------------------- control

    async def set_mode(self, mode: TradingMode) -> None:
        previous = self.order_manager.mode
        self.order_manager.set_mode(mode)
        await self.audit.append("human", "mode.changed",
                                {"from": previous.value, "to": mode.value})
        log.info("operating mode changed", was=previous.value, now=mode.value)

    async def status_report(self) -> dict[str, object]:
        return {
            "version": __version__,
            "mode": self.order_manager.mode.value,
            "market_session": self.clock.session().value,
            "broker": {"name": self.broker.name, "paper": self.broker.is_paper,
                       "connected": self.broker.connected},
            "providers": self.router.provider_status(),
            "risk": self.risk.status(),
            "health": self.health.report(),
            "scheduler": dict(self.scheduler.last_runs),
            "update_available": self.updates.available,
        }

    # -------------------------------------------------------------- lifecycle

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._shutdown.set)
        await self._shutdown.wait()
        await self.stop()

    async def stop(self) -> None:
        log.info("shutting down")
        with contextlib.suppress(Exception):
            await self.audit.append("system", "shutdown", {})
        for closer in (
            self.dashboard.stop, self.updates.stop, self.health.stop,
            self.scheduler.stop, self.sync.stop,
        ):
            with contextlib.suppress(Exception):
                await closer()
        with contextlib.suppress(Exception):
            await self.broker.disconnect()
        with contextlib.suppress(Exception):
            await self.router.close()
        await self.bus.close()
        await self.db.close()
        log.info("shutdown complete")
