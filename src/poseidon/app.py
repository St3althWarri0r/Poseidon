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
import uuid
from datetime import UTC, datetime, time
from pathlib import Path

import structlog
import yaml

from . import __version__
from .ai.agent import ClaudeAgent
from .ai.chat import ChatService
from .ai.reports import render_decision_report
from .ai.tools import ToolDispatcher
from .analytics.performance import FillRecord, build_round_trips, compute_performance
from .api.server import DashboardServer
from .brokers.base import Broker
from .brokers.plugins.paper import PaperBroker
from .brokers.registry import broker_catalog, create_broker
from .core.clock import EASTERN, FreshnessPolicy, MarketClock, calendar_covers
from .core.config import (
    AppConfig,
    BrokerConfig,
    ScheduleConfig,
    default_config_dir,
    local_overlay_path,
)
from .core.container import Container
from .core.enums import HealthState, TradingMode
from .core.errors import AgentError, AgentRefusedError, ConfigError, DataError
from .core.events import EventBus, Topics
from .data.providers import BUILTIN_PROVIDERS
from .data.router import DataRouter
from .execution.approvals import ApprovalQueue
from .execution.guardian import PositionGuardian
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
from .strategy.workshop import AlgorithmWorkshop
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
        self.guardian: PositionGuardian
        self.agent: ClaudeAgent | None = None
        self.chat: ChatService | None = None
        self.strategies: StrategyEngine
        self.workshop: AlgorithmWorkshop
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
        log.info("starting Poseidon", version=__version__, mode=cfg.mode)

        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(cfg.data_dir / "poseidon.db")
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
        # Rehydrate the daily order counter so a mid-session restart cannot
        # silently reset max_orders_per_day. Count from the Eastern day start
        # (in UTC) to match the engine's Eastern-midnight roll.
        eastern_day_start = datetime.combine(
            self.clock.now_eastern().date(), time.min, tzinfo=EASTERN
        ).astimezone(UTC).isoformat()
        self.risk.seed_orders_today(
            await self.order_manager.orders_today_count(eastern_day_start),
            self.clock.now_eastern().date().isoformat(),
        )
        self.guardian = PositionGuardian(cfg.guardian, self.db, self)
        self.bus.subscribe(Topics.ORDER_FILLED, self.guardian.on_order_filled)
        self.bus.subscribe(Topics.CIRCUIT_OPENED, self._on_circuit_opened)
        self.strategies = StrategyEngine(cfg.strategies, cfg.all_watchlist_symbols())
        self.workshop = AlgorithmWorkshop(
            self.db, self.strategies, self.audit,
            default_symbols=cfg.all_watchlist_symbols(),
            sleeve_caps=self.risk.sleeve_caps,
        )
        # Bundled example algorithms: packaged inside poseidon for installed
        # builds (wheel force-include), or the repo-root examples/ for a
        # source/editable checkout.
        bundled = Path(__file__).resolve().parent / "examples" / "algorithms"
        if not bundled.is_dir():
            bundled = Path(__file__).resolve().parents[2] / "examples" / "algorithms"
        await self.workshop.seed_bundled(bundled)
        await self.workshop.load_active()
        dispatcher = ToolDispatcher(
            self.router, self.portfolio, self.risk,
            allow_delayed_quotes=cfg.data.allow_delayed_for_research,
            benchmark_symbol=cfg.risk.benchmark_symbol,
            risk_config=cfg.risk,
            workshop=self.workshop,
        )
        api_key = self.vault.get(cfg.ai.api_key_credential)
        self.agent = ClaudeAgent(cfg.ai, api_key, dispatcher)
        self.chat = ChatService(cfg.ai, self.agent.client, dispatcher, self.db)
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
        log.info("Poseidon is up",
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
            # The key field may be `key_id` (Alpaca), `api_key`, or `secret`
            # (Public — lets one vault entry serve broker + data provider).
            if credential.strip().startswith("{"):
                blob = json.loads(credential)
                # Pop every key-bearing field so secrets never land in options.
                key_fields = [blob.pop(f, None) for f in ("key_id", "api_key", "secret")]
                api_key = next((k for k in key_fields if k), "")
                options.update(blob)
            providers.append(
                (cls(api_key=api_key, timeout=self.config.data.request_timeout_seconds,
                     options=options), provider_cfg.priority)
            )
        if not providers:
            raise ConfigError("no market data providers configured — Poseidon cannot run without live data")
        return DataRouter(
            providers,
            FreshnessPolicy(
                real_time_max_age=self.config.data.real_time_max_age_seconds,
                delayed_max_age=self.config.data.delayed_max_age_seconds,
            ),
        )

    async def _build_broker(self, broker_cfg: BrokerConfig | None = None, *,
                            credentials_override: dict[str, str] | None = None) -> Broker:
        """Construct + connect a broker. With no arguments this builds the
        config's primary broker (startup path); the Account view passes an
        explicit BrokerConfig (and, for a first-time connect, the credentials
        the operator just typed, before they are committed to the vault)."""
        broker_cfg = broker_cfg or self.config.primary_broker()
        if broker_cfg is None:
            # Research mode without a broker: use the paper broker so the
            # portfolio surface still works.
            broker_cfg_options: dict[str, object] = {
                "state_file": str(self.config.data_dir / "paper_state.json")
            }
            broker: Broker = PaperBroker(credentials={}, options=broker_cfg_options)
        else:
            credentials: dict[str, str] = {}
            if credentials_override is not None:
                credentials = credentials_override
            elif broker_cfg.credential:
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

    async def _on_circuit_opened(self, _topic: str, payload: object) -> None:
        """Record automatic circuit-breaker trips in the tamper-evident audit
        chain (the error-rate breaker opens itself; the manual HALT path
        audits separately)."""
        await self.audit.append("system", "circuit.opened",
                                payload if isinstance(payload, dict) else {})

    # ------------------------------------------------------- broker connection

    def _broker_config_for(self, name: str, *, paper: bool) -> BrokerConfig:
        entry = next((e for e in broker_catalog() if e["name"] == name), None)
        if entry is None or not entry.get("connectable"):
            reason = str(entry.get("stub_reason", "")) if entry else "unknown broker"
            raise ConfigError(
                f"'{name}' cannot be connected: {reason or 'no dashboard setup available'}"
            )
        return BrokerConfig(name=name, enabled=True, primary=True,
                            credential=str(entry.get("credential", "")), paper=paper)

    async def broker_connection_test(self, name: str, *, paper: bool,
                                     credentials: dict[str, str] | None) -> dict[str, object]:
        """Prove a broker connection end-to-end (auth + account fetch) without
        touching the active broker, the vault, or any config. ``credentials``
        None means use the credential already stored in the vault."""
        cfg = self._broker_config_for(name, paper=paper)
        broker = await self._build_broker(cfg, credentials_override=credentials)
        try:
            snapshot = await broker.account()
            return {
                "display_name": broker.display_name or broker.name,
                "account_id": snapshot.account_id,
                "equity": str(snapshot.equity),
                "cash": str(snapshot.cash),
                "buying_power": str(snapshot.buying_power),
                "paper": broker.is_paper,
            }
        finally:
            with contextlib.suppress(Exception):
                await broker.disconnect()

    async def switch_broker(self, name: str, *, paper: bool,
                            credentials: dict[str, str] | None) -> dict[str, object]:
        """Connect a brokerage from the Account view and make it the active
        broker, live — no restart. Order of operations is deliberate: the new
        connection is PROVEN first; only then are credentials committed to the
        vault and the choice persisted (poseidon.local.yaml) so a restart
        comes back to the same broker."""
        open_count = await self.order_manager.open_order_count()
        if open_count:
            raise ConfigError(
                f"{open_count} order(s) are still open at "
                f"{self.broker.display_name or self.broker.name} — cancel them or let them "
                "finish before switching brokers"
            )
        cfg = self._broker_config_for(name, paper=paper)
        new_broker = await self._build_broker(cfg, credentials_override=credentials)

        if credentials is not None and cfg.credential:
            await asyncio.to_thread(self.vault.set, cfg.credential, json.dumps(credentials))
        await asyncio.to_thread(self._write_broker_overlay, cfg)

        old = self.broker
        self.broker = new_broker
        self.order_manager.set_broker(new_broker)
        self.sync.set_broker(new_broker)
        self._apply_broker_to_config(cfg)
        # Baselines, peaks, and cached risk metrics belong to the OLD account;
        # carrying them over would fabricate drawdowns/losses on the new one.
        self.portfolio.peak_equity = None
        self.portfolio.day_start_equity = None
        self.portfolio.week_start_equity = None
        self.portfolio.equity_history = []
        self.portfolio.risk_metrics = None
        self.portfolio.risk_metrics_at = None
        await self.db.kv_set("baseline.day.date", "")
        await self.db.kv_set("baseline.week.key", "")
        await self.db.kv_set("baseline.broker", new_broker.name)
        await self.audit.append("human", "broker.switched", {
            "from": old.name, "to": new_broker.name, "paper": new_broker.is_paper,
        })
        with contextlib.suppress(Exception):
            await old.disconnect()
        try:
            await self.sync.sync_once()
        except Exception as exc:
            log.warning("first sync after broker switch failed; sync loop will retry",
                        error=str(exc))
        display = new_broker.display_name or new_broker.name
        await self.bus.publish(Topics.NOTIFY, {
            "level": "info" if new_broker.is_paper else "warning",
            "title": f"Broker switched: {display}",
            "body": (f"Orders now route to {display} "
                     + ("(paper)." if new_broker.is_paper else "(LIVE account).")
                     + f" Operating mode is '{self.order_manager.mode.value}'."),
        })
        acct = self.portfolio.account
        return {
            "name": new_broker.name,
            "display_name": display,
            "paper": new_broker.is_paper,
            "account_id": acct.account_id if acct else None,
            "equity": str(acct.equity) if acct else None,
            "provider_note": ("Public.com real-time market data was also enabled — restart "
                              "Poseidon to activate the data provider." if name == "public" else ""),
        }

    def _apply_broker_to_config(self, cfg: BrokerConfig) -> None:
        others = [b.model_copy(update={"primary": False})
                  for b in self.config.brokers if b.name != cfg.name]
        self.config.brokers = [*others, cfg]

    def _write_broker_overlay(self, cfg: BrokerConfig) -> None:
        """Persist the dashboard's broker choice to poseidon.local.yaml (merged
        over the main config at startup). Secrets never land here — only the
        vault credential NAME."""
        path = self.config.config_path or default_config_dir() / "poseidon.yaml"
        overlay_file = local_overlay_path(path)
        existing: dict[str, object] = {}
        if overlay_file.exists():
            loaded = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        brokers_raw = existing.get("brokers")
        brokers = [dict(b) for b in brokers_raw if isinstance(b, dict)] \
            if isinstance(brokers_raw, list) else []
        brokers = [{**b, "primary": False} for b in brokers if b.get("name") != cfg.name]
        brokers.append({"name": cfg.name, "enabled": True, "primary": True,
                        "paper": cfg.paper, "credential": cfg.credential})
        existing["brokers"] = brokers
        if cfg.name == "public":
            # The same secret powers Public's free real-time data — enable it.
            data_raw = existing.get("data")
            data = dict(data_raw) if isinstance(data_raw, dict) else {}
            providers_raw = data.get("providers")
            providers = [dict(p) for p in providers_raw if isinstance(p, dict)] \
                if isinstance(providers_raw, list) else []
            if not any(p.get("name") == "public_data" for p in providers):
                providers.append({"name": "public_data", "credential": "public_api_secret",
                                  "priority": 10, "enabled": True})
            data["providers"] = providers
            existing["data"] = data
        header = (
            "# Managed by the Poseidon dashboard (Account view).\n"
            "# Broker connections chosen in the UI persist here and are merged over\n"
            "# poseidon.yaml at startup. Delete this file to revert to the main config.\n"
            "# SECRETS ARE NEVER STORED HERE — credentials live in the encrypted vault.\n"
        )
        overlay_file.parent.mkdir(parents=True, exist_ok=True)
        overlay_file.write_text(header + yaml.safe_dump(existing, sort_keys=False),
                                encoding="utf-8")

    # ------------------------------------------------------------------- chat

    async def chat_message(self, message: str) -> dict[str, object]:
        """One operator chat turn, budget-gated and usage-metered exactly like
        a review cycle."""
        if self.chat is None:
            raise ConfigError("AI is not configured")
        if await self._over_ai_budget():
            return {"reply": ("The monthly AI budget (ai.monthly_budget_usd) is exhausted, so "
                              "chat and review cycles are paused until next month. Raise or "
                              "remove the budget in poseidon.yaml to keep talking."),
                    "tool_calls": [], "usage": {}}
        result = await self.chat.send(message, context=self._chat_context())
        usage = result.get("usage")
        if isinstance(usage, dict) and usage.get("api_calls"):
            await self.db.execute(
                "INSERT OR REPLACE INTO ai_usage (cycle_id, at, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, api_calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"chat-{uuid.uuid4().hex[:8]}", datetime.now(UTC).isoformat(),
                 usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                 usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0),
                 usage.get("api_calls", 0)),
            )
        return result

    def _chat_context(self) -> str:
        acct = self.portfolio.account
        positions = ", ".join(
            f"{p.symbol}×{p.quantity}" for p in self.portfolio.positions[:20]
        ) or "none"
        broker_label = self.broker.display_name or self.broker.name
        return "\n".join([
            f"utc_time: {datetime.now(UTC).isoformat(timespec='seconds')}",
            f"operating_mode: {self.order_manager.mode.value}",
            f"market_session: {self.clock.session().value}",
            f"broker: {broker_label}" + (" (paper)" if self.broker.is_paper else " (LIVE)"),
            f"equity: {acct.equity if acct else 'not synced yet'}",
            f"cash: {acct.cash if acct else 'n/a'}",
            f"buying_power: {acct.buying_power if acct else 'n/a'}",
            f"positions: {positions}",
            f"circuit_breaker: {'OPEN — trading halted' if self.risk.circuit.is_open else 'closed'}",
        ])

    def _register_jobs(self) -> None:
        self.scheduler.register_job("review_cycle", self.run_review_cycle)
        self.scheduler.register_job("portfolio_sync", self.sync.sync_once)
        self.scheduler.register_job("update_check", self._update_check_job)
        self.scheduler.register_job("audit_verify", self._audit_verify_job)
        self.scheduler.register_job("position_guardian", self.guardian.check_all)
        self.scheduler.register_job("daily_report", self.send_daily_report)
        self.scheduler.register_job("risk_metrics", self._risk_metrics_job)

    def _effective_schedules(self) -> list[ScheduleConfig]:
        """Config schedules plus a default review cadence if none is defined."""
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
        if self.config.guardian.enabled and not any(
            s.job == "position_guardian" and s.enabled for s in schedules
        ):
            schedules.append(
                ScheduleConfig(name="position-guardian", job="position_guardian",
                               every_seconds=self.config.guardian.interval_seconds,
                               only_market_hours=True)
            )
        if self.config.reports.daily_summary and not any(
            s.job == "daily_report" and s.enabled for s in schedules
        ):
            schedules.append(
                ScheduleConfig(name="daily-summary", job="daily_report",
                               cron=self.config.reports.daily_summary_cron)
            )
        if not any(s.job == "risk_metrics" and s.enabled for s in schedules):
            schedules.append(
                ScheduleConfig(name="risk-metrics", job="risk_metrics",
                               every_seconds=900, only_market_hours=True)
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
                return HealthState.UNHEALTHY, "holiday calendar does not cover today — update Poseidon"
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
            if await self._over_ai_budget():
                log.warning("monthly AI budget reached; skipping review cycle")
                await self.bus.publish(Topics.NOTIFY, {
                    "level": "warning", "title": "AI budget reached",
                    "body": f"Estimated spend hit ai.monthly_budget_usd "
                            f"(${self.config.ai.monthly_budget_usd:.2f}); review cycles are "
                            "paused until next month or a higher budget.",
                })
                return
            started = datetime.now(UTC)
            try:
                signals = await self.strategies.scan_all(self.router, self.portfolio)
                # Trusted attribution for sleeve caps: only symbols a sleeved
                # strategy actually signalled this cycle get its cap.
                self.risk.set_cycle_attribution(list(signals))
                decision = await self.agent.run_cycle(
                    mode=self.order_manager.mode,
                    watchlist=self.config.all_watchlist_symbols(),
                    enabled_strategies=self.strategies.enabled_names,
                    strategy_signals=[s.as_dict() for s in signals],
                    market_session=self.clock.session().value,
                    market_regime=await self._regime_line(),
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
            if decision.usage:
                u = decision.usage
                await self.db.execute(
                    "INSERT OR REPLACE INTO ai_usage (cycle_id, at, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens, api_calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (decision.cycle_id, (decision.created_at or started).isoformat(),
                     u.get("input_tokens", 0), u.get("output_tokens", 0),
                     u.get("cache_read_tokens", 0), u.get("cache_write_tokens", 0),
                     u.get("api_calls", 0)),
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

    async def _risk_metrics_job(self) -> None:
        await self.refresh_risk_metrics()

    async def _regime_line(self) -> str | None:
        """Regime summary for the cycle prompt. Uses the cached metrics when
        fresh; otherwise computes from live benchmark bars alone (one call).
        Returns None when history is unavailable — the AI is told nothing
        rather than something stale."""
        from .analytics.regime import compute_regime

        metrics = self.portfolio.risk_metrics
        age = self.portfolio.risk_metrics_age_seconds()
        if metrics is not None and age is not None and age < 1800:
            regime = metrics.get("regime")
            if isinstance(regime, dict) and regime.get("state") not in (None, "unknown"):
                return (f"{regime['state']} ({regime.get('detail', '')}) — "
                        f"benchmark {regime.get('benchmark')}")
        try:
            bars = await self.router.bars(self.config.risk.benchmark_symbol,
                                          timeframe="1d", limit=300)
        except DataError:
            return None
        report = compute_regime([float(b.close) for b in bars],
                                benchmark=self.config.risk.benchmark_symbol)
        return report.summary_line() if report.state != "unknown" else None

    async def refresh_risk_metrics(self) -> dict[str, object]:
        """Recompute portfolio VaR/beta/correlation from live bar history
        and cache it on the portfolio state (scheduled job + API/tool)."""
        from .analytics.risk_metrics import gather_risk_metrics

        report = await gather_risk_metrics(
            self.router, self.portfolio, benchmark=self.config.risk.benchmark_symbol
        )
        payload = report.as_dict()
        self.portfolio.risk_metrics = payload
        self.portfolio.risk_metrics_at = report.as_of
        return payload

    async def review_algorithm(self, *, source: str, instructions: str = "") -> dict[str, object]:
        """Claude reviews pasted external algorithm code and (when possible)
        converts it to the workshop contract. Token usage is metered like a
        review cycle."""
        from .ai.reviewer import review_algorithm

        if self.agent is None:
            raise ConfigError("AI agent is not initialized")
        review = await review_algorithm(
            self.agent.client, self.config.ai.model,
            source=source, instructions=instructions,
        )
        usage = review.pop("usage", {})
        await self.db.execute(
            "INSERT INTO ai_usage (cycle_id, at, input_tokens, output_tokens, api_calls) "
            "VALUES (?, ?, ?, ?, 1)",
            (f"algo-review-{uuid.uuid4().hex[:8]}", datetime.now(UTC).isoformat(),
             int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
        )
        await self.audit.append("claude", "algorithm.reviewed",
                                {"convertible": review.get("convertible"),
                                 "suggested_name": review.get("suggested_name")})
        return review

    async def execution_report(self, *, limit: int = 500) -> dict[str, object]:
        """Transaction cost analysis over the platform's own order records."""
        from .analytics.execution import execution_quality

        rows = await self.db.fetch_all(
            "SELECT payload FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return execution_quality([json.loads(r[0]) for r in rows])

    # ------------------------------------------------------- analytics & cost

    async def _over_ai_budget(self) -> bool:
        budget = self.config.ai.monthly_budget_usd
        if budget <= 0:
            return False
        summary = await self.ai_usage_summary()
        month_cost = summary["month_cost_usd"]
        assert isinstance(month_cost, float)  # ai_usage_summary computes it as float
        return month_cost >= budget

    async def ai_usage_summary(self) -> dict[str, object]:
        """Token totals and estimated spend for the current calendar month."""
        month_prefix = datetime.now(UTC).strftime("%Y-%m")
        row = await self.db.fetch_one(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0), "
            "COALESCE(SUM(api_calls),0), COUNT(*) FROM ai_usage WHERE at LIKE ?",
            (f"{month_prefix}%",),
        )
        input_tokens, output_tokens, cache_read, cache_write, api_calls, cycles = row or (0,) * 6
        cfg = self.config.ai
        # Cache reads bill ~0.1x input; cache writes ~1.25x — close enough for
        # budget gating (the exact bill is on the provider's console).
        cost = (
            input_tokens * cfg.input_price_per_mtok
            + cache_read * cfg.input_price_per_mtok * 0.1
            + cache_write * cfg.input_price_per_mtok * 1.25
            + output_tokens * cfg.output_price_per_mtok
        ) / 1_000_000
        return {
            "month": month_prefix,
            "cycles": cycles,
            "api_calls": api_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "month_cost_usd": round(cost, 2),
            "monthly_budget_usd": cfg.monthly_budget_usd or None,
        }

    async def performance_report(self) -> dict[str, object]:
        """Portfolio metrics from stored equity marks + realized round trips
        from the platform's own filled orders, attributed per strategy."""
        from decimal import Decimal

        from .core.enums import OrderSide, OrderStatus
        from .core.models import Order

        marks = await self.db.fetch_all(
            "SELECT at, equity FROM equity_marks WHERE broker = ? ORDER BY at ASC",
            (self.broker.name,),
        )
        equity_points = [(datetime.fromisoformat(r[0]), float(r[1])) for r in marks]
        rows = await self.db.fetch_all(
            "SELECT payload FROM orders WHERE status = ? ORDER BY updated_at ASC",
            (OrderStatus.FILLED.value,),
        )
        fills: list[FillRecord] = []
        for (payload,) in rows:
            order = Order.model_validate(json.loads(payload))
            if order.filled_quantity <= 0 or order.avg_fill_price is None:
                continue
            fills.append(FillRecord(
                symbol=order.symbol, side=OrderSide(order.side),
                quantity=Decimal(str(order.filled_quantity)),
                price=Decimal(str(order.avg_fill_price)),
                at=order.updated_at or datetime.now(UTC),
                strategy=order.strategy,
            ))
        trips = build_round_trips(fills)
        report = compute_performance(equity_points, trips).as_dict()
        report["open_exit_plans"] = await self.guardian.active_plans()
        return report

    async def send_daily_report(self) -> None:
        """End-of-day digest through the notification channels."""
        performance = await self.performance_report()
        usage = await self.ai_usage_summary()
        today = self.clock.now_eastern().date().isoformat()
        orders_today = [
            o for o in await self.order_manager.recent_orders(200)
            if (o.get("created_at") or "").startswith(today)
        ]
        filled = [o for o in orders_today if o.get("status") == "filled"]
        rejected = [o for o in orders_today if str(o.get("status", "")).startswith("rejected")]
        account = self.portfolio.account
        lines = [
            f"Poseidon daily summary — {today}",
            "",
            f"Equity: {account.equity if account else 'n/a'}  "
            f"(day P&L: {account.day_pnl if account else 'n/a'})",
            f"Day loss used: {self.portfolio.day_loss_pct():.2%} · "
            f"Drawdown: {self.portfolio.drawdown_pct():.2%}",
            f"Orders today: {len(orders_today)} ({len(filled)} filled, {len(rejected)} rejected)",
            f"Total return: {performance['total_return']:.2%} · Sharpe: {performance['sharpe']} · "
            f"Win rate: {performance['win_rate']:.0%} over {performance['trades']} closed trades",
            f"AI spend this month: ~${usage['month_cost_usd']} "
            f"({usage['cycles']} cycles, {usage['api_calls']} API calls)",
        ]
        if filled:
            lines.append("")
            lines.append("Fills:")
            lines += [
                f"  {o.get('side')} {o.get('filled_quantity')} {o.get('symbol')} "
                f"@ {o.get('avg_fill_price')}" for o in filled[:10]
            ]
        await self.bus.publish(Topics.NOTIFY, {
            "level": "info", "title": f"Daily summary {today}", "body": "\n".join(lines),
        })

    async def _update_check_job(self) -> None:
        await self.updates.check_once()

    async def _audit_verify_job(self) -> None:
        ok, bad_seq = await self.audit.verify_chain()
        if not ok:
            # force_open() does not publish CIRCUIT_OPENED (unlike the
            # error-rate auto-trip), so record this halt in the chain directly.
            await self.audit.append("system", "circuit.opened",
                                    {"reason": "audit chain corrupt", "bad_seq": bad_seq,
                                     "source": "audit_verify"})
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
            "ai_usage": await self.ai_usage_summary(),
            "guardian": {
                "enabled": self.config.guardian.enabled,
                "active_plans": await self.guardian.active_plans(),
            },
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
