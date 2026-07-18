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
from .ai.analysis_service import AnalysisService
from .ai.backends import ChatBackend, build_backends
from .ai.chat import ChatService
from .ai.hardware import DEFAULT_LM_STUDIO_URL, probe_local_models
from .ai.reflection_service import ReflectionService
from .ai.reports import render_decision_report
from .ai.tools import ToolDispatcher
from .analytics.decay_service import StrategyHealthService
from .analytics.performance import FillRecord, RoundTrip, build_round_trips, compute_performance
from .api.server import DashboardServer
from .brokers.base import Broker
from .brokers.plugins.paper import PaperBroker
from .brokers.registry import broker_catalog, create_broker
from .core.clock import EASTERN, FreshnessPolicy, MarketClock, calendar_covers
from .core.config import (
    AIConfig,
    AppConfig,
    BrokerConfig,
    ScheduleConfig,
    default_config_dir,
    local_overlay_path,
)
from .core.container import Container
from .core.enums import HealthState, TradingMode
from .core.errors import (
    AgentError,
    AgentRefusedError,
    BackendUnreachableError,
    ConfigError,
    DataError,
    VaultError,
)
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

_OVERLAY_HEADER = (
    "# Managed by the Poseidon dashboard (Account view).\n"
    "# Broker connections chosen in the UI persist here and are merged over\n"
    "# poseidon.yaml at startup. Delete this file to revert to the main config.\n"
    "# SECRETS ARE NEVER STORED HERE — credentials live in the encrypted vault.\n"
)


def _env_credential(entry: dict[str, object], paper: bool) -> str:
    """Vault credential name for a broker's paper/live environment.

    Alpaca is the only broker whose paper and live accounts are distinct with
    their own API keys; its catalog entry carries ``credential_paper`` and
    ``credential_live``. When those are absent (every other broker) this falls
    back to the single ``credential`` name — unchanged behaviour."""
    env_key = "credential_paper" if paper else "credential_live"
    return str(entry.get(env_key) or entry.get("credential", ""))


class ApplicationKernel:
    def __init__(self, config: AppConfig, vault: Vault) -> None:
        self.config = config
        self.vault = vault
        self.container = Container()
        self.bus = EventBus()
        self.clock = MarketClock()
        self.portfolio = PortfolioState()
        # Advisory reflection loop; the real service is built in start().
        self.reflection: ReflectionService | None = None
        # Advisory analyst-firm -> debate-packet loop; the real service is
        # built in start(). Packets it produces are injected into the PM's
        # cycle prompt only — never the risk engine, the order path, or chat.
        self.analysis: AnalysisService | None = None
        # Advisory strategy-decay watchdog; the real service is built in
        # start(). Reduce-only: its one mutation is deactivating a decayed
        # CUSTOM strategy — it never activates one or touches the order path.
        self.strategy_health: StrategyHealthService | None = None

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
        self._backend: ChatBackend | None = None
        # Utility backend for the tolerant auxiliary roles (chat, reflection).
        # IS the primary object unless ai.utility_model configures tiering.
        self._utility_backend: ChatBackend | None = None
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
        # A pre-2.4.0 audit log was written with the previous hash encoding and
        # re-verifies as "bad" purely because of the format change. Only
        # re-anchor it when it is fully intact under a known legacy encoding (a
        # genuinely tampered log still fails); this preserves the tamper-evidence
        # guarantee while letting existing installs upgrade.
        if not ok and await self.audit.migrate_legacy_chain():
            ok, bad_seq = await self.audit.verify_chain()
            if ok:
                log.info("migrated audit log to the current hash encoding")
                await self.audit.append("system", "audit.chain_migrated",
                                        {"from": "v1", "reason": "hash-encoding upgrade"})
        if not ok:
            raise ConfigError(
                f"audit chain verification FAILED at seq {bad_seq} — the audit log has been "
                "tampered with or corrupted; refusing to start (see docs/troubleshooting.md)"
            )

        migrated_to = await self._migrate_legacy_alpaca_credential()
        if migrated_to is not None:
            log.info("migrated legacy alpaca_keys to env-scoped credential",
                     credential=migrated_to)

        self.router = self._build_router()
        self.broker = await self._build_broker()
        self.risk = RiskEngine(cfg.risk, self.portfolio, self.router, self.clock, self.bus,
                               halt_file=cfg.data_dir / "HALT")
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
        # An operator HALT (dashboard) that was active before a restart must be
        # re-armed — the breaker's manual latch is otherwise memory-only.
        await self._restore_manual_halt()
        self.guardian = PositionGuardian(cfg.guardian, self.db, self)
        self.bus.subscribe(Topics.ORDER_FILLED, self.guardian.on_order_filled)
        # Unfilled guardian exits (e.g. a DAY limit that expires at the close)
        # surface as ORDER_UPDATED — the guardian re-arms the plan from these.
        self.bus.subscribe(Topics.ORDER_UPDATED, self.guardian.on_order_update)
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
        # Chat gets its OWN dispatcher: the review cycle clears and snapshots
        # dispatcher.sources_used into each decision's data_sources, and a
        # concurrent chat tool call must not inject provenance into that
        # audited record.
        chat_dispatcher = ToolDispatcher(
            self.router, self.portfolio, self.risk,
            allow_delayed_quotes=cfg.data.allow_delayed_for_research,
            benchmark_symbol=cfg.risk.benchmark_symbol,
            risk_config=cfg.risk,
            workshop=self.workshop,
        )
        self._wire_ai(cfg.ai, dispatcher, chat_dispatcher)
        self.notifier = NotificationService(cfg.notifications, self.vault, self.bus)
        self.sync = PortfolioSyncService(self.broker, self.portfolio, self.bus, self.db, self.clock)
        self.strategy_health = StrategyHealthService(
            db=self.db, config=cfg.strategy_health,
            load_trips=self._load_strategy_trips,
            audit_append=self.audit.append,
            notify=lambda level, data: self.bus.publish(Topics.NOTIFY, {
                "level": level,
                "title": f"strategy health: {data.get('strategy')} -> {data.get('state')}",
                "body": f"rolling-window return {data.get('window_return', 0.0):+.2%}",
            }),
            retire=self._retire_strategy)
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

    def _wire_ai(self, ai_cfg: AIConfig, dispatcher: ToolDispatcher,
                 chat_dispatcher: ToolDispatcher) -> None:
        """Construct the AI roles and bind each to its model tier.

        INVARIANT: the trading agent (the money decision) and the algorithm
        reviewer always use the PRIMARY backend; the advisory chat, reflection,
        and analysis roles use the utility backend. With no ``ai.utility_model``
        the utility backend IS the primary object, so every role shares one
        backend exactly as before — this is the default that ships.

        Cost control: the advisory roles receive the same contract chat and
        review cycles enforce inline — their spend is metered into ``ai_usage``
        (role-tagged) and their sweeps skip once ``_over_ai_budget`` trips.
        """
        self._backend, self._utility_backend = build_backends(ai_cfg, self.vault.get)
        self.agent = ClaudeAgent(ai_cfg, self._backend, dispatcher)
        # Advisory post-trade reflection loop (never gates risk / touches orders).
        self.reflection = ReflectionService(
            db=self.db, router=self.router, config=ai_cfg.reflection, model=ai_cfg.model,
            get_backend=lambda: self._utility_backend,
            load_fills=self._load_reflection_fills, is_flat=self._symbol_is_flat,
            audit_append=self.audit.append,
            record_usage=lambda usage: self._record_ai_usage(usage, "reflection"),
            over_budget=self._over_ai_budget)
        self.bus.subscribe(Topics.ACCOUNT_SYNCED, self.reflection.on_account_synced)
        # Advisory analyst firm -> debate packet (never gates risk / touches
        # orders). Packets are injected into the PM's cycle prompt only — see
        # ai/agent.py's _cycle_prompt analysis_block.
        self.analysis = AnalysisService(
            db=self.db, router=self.router, config=ai_cfg.analysis, model=ai_cfg.model,
            get_backend=lambda: self._utility_backend,
            watchlist=lambda: self.config.all_watchlist_symbols(),
            audit_append=self.audit.append,
            # v1: scan=None — no untrusted text flows yet (context=""); wire
            # ai/tools.py's injection scanner here when the per-role
            # news/fundamentals retrieval fast-follow lands.
            scan=None,
            record_usage=lambda usage: self._record_ai_usage(usage, "analysis"),
            over_budget=self._over_ai_budget)
        self.chat = ChatService(ai_cfg, self._utility_backend, chat_dispatcher, self.db)

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
        if (broker.rotated_credentials and broker_cfg is not None and broker_cfg.credential
                and credentials_override is None):
            # A single-use credential (e.g. a tastytrade remember token) was
            # consumed and replaced during connect; persist the replacement or
            # the next vault-based connect fails auth.
            await asyncio.to_thread(
                self.vault.set, broker_cfg.credential, json.dumps(broker.rotated_credentials)
            )
        return broker

    async def _on_circuit_opened(self, _topic: str, payload: object) -> None:
        """Record automatic circuit-breaker trips in the tamper-evident audit
        chain (the error-rate breaker opens itself; the manual HALT path
        audits separately)."""
        await self.audit.append("system", "circuit.opened",
                                payload if isinstance(payload, dict) else {})

    # ------------------------------------------------------- broker connection

    def _broker_config_for(self, name: str, *, paper: bool,
                           options: dict[str, object] | None = None) -> BrokerConfig:
        entry = next((e for e in broker_catalog() if e["name"] == name), None)
        if entry is None or not entry.get("connectable"):
            reason = str(entry.get("stub_reason", "")) if entry else "unknown broker"
            raise ConfigError(
                f"'{name}' cannot be connected: {reason or 'no dashboard setup available'}"
            )
        # Inherit what the operator already configured in poseidon.yaml for
        # this broker (options like ibkr's gateway_url, or a custom credential
        # name) — the dashboard switch must not silently discard them. Form
        # options (paper starting_cash, reset) layer on top.
        existing = next((b for b in self.config.brokers if b.name == name), None)
        # Credential is ENV-scoped: a matching-env config entry (name + paper +
        # a saved credential) wins so an operator's custom vault name is kept;
        # otherwise resolve the catalog's per-env name. Matching by name ALONE
        # would hand a live switch the paper credential (or vice-versa) whenever
        # the config holds only the opposite-env entry — a real-money hazard.
        existing_env = next((b for b in self.config.brokers
                             if b.name == name and b.paper == paper and b.credential), None)
        credential = (existing_env.credential if existing_env
                      else _env_credential(entry, paper))
        merged_options: dict[str, object] = dict(existing.options) if existing else {}
        merged_options.update(options or {})
        return BrokerConfig(name=name, enabled=True, primary=True,
                            credential=credential, paper=paper, options=merged_options)

    async def broker_connection_test(self, name: str, *, paper: bool,
                                     credentials: dict[str, str] | None,
                                     options: dict[str, object] | None = None) -> dict[str, object]:
        """Prove a broker connection end-to-end (auth + account fetch) without
        touching the active broker or config. ``credentials`` None means use the
        credential already stored in the vault. Note: a connect can consume a
        single-use credential (e.g. a tastytrade remember token); when testing
        with the stored credential its rotated replacement is re-persisted so a
        following Connect still authenticates."""
        cfg = self._broker_config_for(name, paper=paper, options=options)
        if name == "paper":
            # The test instance shares the active paper broker's state file;
            # it may read the simulated account but must never write it back
            # (a requested reset shows the fresh numbers without committing).
            cfg.options["read_only"] = True
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
                            credentials: dict[str, str] | None,
                            options: dict[str, object] | None = None) -> dict[str, object]:
        """Connect a brokerage from the Account view and make it the active
        broker, live — no restart.

        Ordering is deliberate and race-guarded: (1) new order pipelines are
        refused and in-flight ones drained (an order decided against one
        account must never reach another); (2) with the world quiet, the
        open-order guard is re-checked; (3) the new connection is PROVEN;
        (4) only then are credentials committed to the vault and the choice
        persisted; (5) the swap itself, state resets, and a fresh sync."""
        await self.order_manager.begin_broker_switch()
        try:
            open_count = await self.order_manager.open_order_count()
            if open_count:
                raise ConfigError(
                    f"{open_count} order(s) are still open at "
                    f"{self.broker.display_name or self.broker.name} — cancel them or let "
                    "them finish before switching brokers"
                )
            cfg = self._broker_config_for(name, paper=paper, options=options)
            new_broker = await self._build_broker(cfg, credentials_override=credentials)
            # A simulator reset is a one-shot action, never persisted config —
            # a restart must not silently wipe the paper book again.
            cfg.options.pop("reset", None)
            try:
                if credentials is not None and cfg.credential:
                    # Persist the post-rotation credential if connect() replaced
                    # a single-use one, else the typed value.
                    await asyncio.to_thread(
                        self.vault.set, cfg.credential,
                        json.dumps(new_broker.rotated_credentials or credentials))
                await asyncio.to_thread(self._write_broker_overlay, cfg)
            except Exception as exc:
                # Persisting failed: nothing was swapped — drop the proven
                # connection and surface a clean, actionable error.
                with contextlib.suppress(Exception):
                    await new_broker.disconnect()
                if isinstance(exc, ConfigError | VaultError):
                    raise
                raise ConfigError(f"could not persist the broker switch: {exc}") from exc

            old = self.broker
            self.broker = new_broker
            self.order_manager.set_broker(new_broker)
            if not new_broker.is_paper and self.order_manager.mode is TradingMode.AUTONOMOUS:
                # A real-money account just became the active broker while armed
                # for autonomous trading. Demote to APPROVAL server-side so a
                # mis-click (or any HTTP caller) can never auto-execute real
                # money — the operator must deliberately re-arm Autonomous.
                # Done HERE, still inside the switch guard (_switching is True so
                # orders are refused and synced_at is None), so there is no
                # window where the LIVE broker is live, the pipeline is reopened,
                # and the mode is still AUTONOMOUS — a concurrent scheduler
                # decision or guardian exit cannot slip a real order in before
                # the clamp. Demotion-only: RESEARCH/APPROVAL are never raised,
                # and a paper switch never clamps the mode. set_mode() writes the
                # mode.changed audit record; the notify build below still reads
                # the already-demoted mode string, so it stays accurate.
                await self.set_mode(TradingMode.APPROVAL)
            await self.sync.set_broker(new_broker)
            self._apply_broker_to_config(cfg)
            # Everything account-scoped belongs to the OLD account. Clear the
            # snapshot (synced_at=None makes the risk engine refuse to trade
            # until the new account's first successful sync), drop cached risk
            # metrics, disarm guardian plans, and reload this account's OWN
            # baselines/peak from its scoped history.
            self.portfolio.account = None
            self.portfolio.positions = []
            self.portfolio.open_orders = []
            self.portfolio.recent_fills = []
            self.portfolio.tax_lots = []
            self.portfolio.dividends = []
            self.portfolio.synced_at = None
            self.portfolio.day_start_equity = None
            self.portfolio.week_start_equity = None
            self.portfolio.peak_equity = None
            self.portfolio.day_min_equity = None
            self.portfolio.week_min_equity = None
            self.portfolio.risk_metrics = None
            self.portfolio.risk_metrics_at = None
            await self.db.execute(
                "UPDATE exit_plans SET active = 0, triggered_reason = 'broker switched', "
                "updated_at = ? WHERE active = 1",
                (datetime.now(UTC).isoformat(),),
            )
            await self.sync.restore_baselines()
            await self.audit.append("human", "broker.switched", {
                "from": old.name, "to": new_broker.name, "paper": new_broker.is_paper,
            })
            if isinstance(old, PaperBroker) and isinstance(new_broker, PaperBroker):
                # Both share the simulator state file; the NEW instance owns
                # it now (it may have just been reset) — the old one's
                # disconnect must not write its stale book back.
                old.make_read_only()
            if isinstance(new_broker, PaperBroker) and (options or {}).get("reset"):
                # The switch is fully persisted — NOW commit the fresh book.
                new_broker.commit_state()
            with contextlib.suppress(Exception):
                await old.disconnect()
        finally:
            self.order_manager.end_broker_switch()
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
            try:
                loaded = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(
                    f"cannot parse {overlay_file}: {exc} — fix or delete the file and retry"
                ) from exc
            if isinstance(loaded, dict):
                existing = loaded
        brokers_raw = existing.get("brokers")
        brokers = [dict(b) for b in brokers_raw if isinstance(b, dict)] \
            if isinstance(brokers_raw, list) else []
        brokers = [{**b, "primary": False} for b in brokers if b.get("name") != cfg.name]
        entry: dict[str, object] = {"name": cfg.name, "enabled": True, "primary": True,
                                    "paper": cfg.paper, "credential": cfg.credential}
        if cfg.options:
            entry["options"] = dict(cfg.options)
        brokers.append(entry)
        existing["brokers"] = brokers
        if cfg.name == "public" and not any(
            p.name == "public_data" for p in self.config.data.providers
        ):
            # The same secret powers Public's free real-time data — enable it,
            # but never override a public_data entry the operator already
            # tuned in poseidon.yaml (priority/options stay theirs).
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
        self._save_overlay(overlay_file, existing)

    def _write_ai_overlay(self, cfg: AIConfig, *, clear_utility: bool = False) -> None:
        """Persist the dashboard's AI backend/model choice to poseidon.local.yaml
        (merged over the main config at startup). Mirrors
        ``_write_broker_overlay``: read the existing overlay (parse error →
        ConfigError), set only the managed ``ai`` sub-block, atomic-write.

        Secrets never land here — only the backend id, model id, and base_url.
        The vault credential is referenced by NAME in the base config and is
        never copied into the overlay.

        ``clear_utility`` writes an explicit ``utility_model: null`` so the
        startup deep-merge overrides a base ``ai.utility_model``. It is set only
        when a backend change cleared the (now cross-backend-stale) utility
        model; on a same-backend model change the key is omitted so a base
        value survives untouched."""
        path = self.config.config_path or default_config_dir() / "poseidon.yaml"
        overlay_file = local_overlay_path(path)
        existing: dict[str, object] = {}
        if overlay_file.exists():
            try:
                loaded = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(
                    f"cannot parse {overlay_file}: {exc} — fix or delete the file and retry"
                ) from exc
            if isinstance(loaded, dict):
                existing = loaded
        ai_block: dict[str, object | None] = {
            "backend": cfg.backend,
            "model": cfg.model,
            "base_url": cfg.base_url,
        }
        if clear_utility:
            ai_block["utility_model"] = None
        existing["ai"] = ai_block
        self._save_overlay(overlay_file, existing)

    @staticmethod
    def _save_overlay(overlay_file: Path, existing: dict[str, object]) -> None:
        overlay_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash mid-write must not leave a truncated overlay that
        # bricks the next startup.
        tmp = overlay_file.with_name(overlay_file.name + ".tmp")
        tmp.write_text(_OVERLAY_HEADER + yaml.safe_dump(existing, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(overlay_file)

    async def _migrate_legacy_alpaca_credential(self) -> str | None:
        """One-time, idempotent migration of the pre-toggle single
        ``alpaca_keys`` vault credential to the env-scoped
        ``alpaca_paper_keys`` / ``alpaca_live_keys`` names the paper/live toggle
        resolves.

        Fires only when the alpaca BROKER is still configured on the legacy
        ``alpaca_keys`` name and neither env name exists yet; the credential is
        COPIED (never moved) into the current-env name and the broker config +
        overlay are repointed. ``alpaca_keys`` is RETAINED because the Alpaca
        *data provider* references it as its ``ProviderConfig.credential`` —
        deleting it would break market data. Returns the new credential name
        when a migration ran, else ``None`` (nothing to do / already migrated).
        """
        if not self.vault.unlocked:
            return None  # locked vault: can't enumerate names — graceful no-op
        # Anchor on the alpaca broker still carrying the legacy name; a
        # data-provider-only ``alpaca_keys`` (no alpaca broker) is NOT migrated,
        # and an already env-scoped broker is left alone. This is also what tells
        # us which env (paper/live) the legacy account was, and which overlay
        # entry to repoint.
        legacy = next((b for b in self.config.brokers
                       if b.name == "alpaca" and b.credential == "alpaca_keys"), None)
        if legacy is None:
            return None
        names = set(self.vault.names())
        if "alpaca_keys" not in names:
            return None  # nothing to copy from
        if "alpaca_paper_keys" in names or "alpaca_live_keys" in names:
            return None  # already env-scoped — idempotent skip
        target = "alpaca_paper_keys" if legacy.paper else "alpaca_live_keys"
        # Copy within the vault (secret value never leaves it); legacy retained.
        await asyncio.to_thread(self.vault.set, target, self.vault.get("alpaca_keys"))
        migrated = legacy.model_copy(update={"credential": target})
        self.config.brokers = [migrated if b is legacy else b
                               for b in self.config.brokers]
        await asyncio.to_thread(self._persist_migrated_broker_credential, migrated)
        await self.audit.append("system", "broker.credential_migrated", {
            "broker": "alpaca", "from": "alpaca_keys", "to": target,
            "paper": legacy.paper,
        })
        return target

    def _persist_migrated_broker_credential(self, cfg: BrokerConfig) -> None:
        """Repoint the alpaca broker overlay entry's credential NAME, preserving
        its primary/enabled/paper/options. Unlike ``_write_broker_overlay`` this
        does NOT force the entry primary — the migration must never change which
        broker trades. Secrets never land here (only the vault name)."""
        path = self.config.config_path or default_config_dir() / "poseidon.yaml"
        overlay_file = local_overlay_path(path)
        existing: dict[str, object] = {}
        if overlay_file.exists():
            try:
                loaded = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(
                    f"cannot parse {overlay_file}: {exc} — fix or delete the file and retry"
                ) from exc
            if isinstance(loaded, dict):
                existing = loaded
        brokers_raw = existing.get("brokers")
        brokers = [dict(b) for b in brokers_raw if isinstance(b, dict)] \
            if isinstance(brokers_raw, list) else []
        others = [b for b in brokers if b.get("name") != cfg.name]
        if cfg.primary:
            others = [{**b, "primary": False} for b in others]
        entry: dict[str, object] = {"name": cfg.name, "enabled": cfg.enabled,
                                    "primary": cfg.primary, "paper": cfg.paper,
                                    "credential": cfg.credential}
        if cfg.options:
            entry["options"] = dict(cfg.options)
        existing["brokers"] = [*others, entry]
        self._save_overlay(overlay_file, existing)

    async def apply_ai_config(self, *, backend: str, model: str) -> dict[str, object]:
        """Live-swap the portfolio-manager brain between the Claude API and a
        local LM Studio backend (and/or change the model within one). Config-only:
        touches ``ai.*``, the overlay, and the backend objects — never the order
        path, the operating mode, or any secret.

        Ordering mirrors ``switch_broker`` — PROVE the target is usable before
        touching anything, then commit: switching TO the Claude API requires the
        anthropic key to already be in the vault, and switching TO local requires
        the endpoint to be reachable; a bad request raises here with the old
        backend still live, never half-switching into a dead brain. The swap
        itself runs under ``_cycle_lock`` so a decision already in flight (a
        multi-round tool loop holds the lock for its whole duration) finishes
        entirely on its original backend/model and the swap can never land
        mid-cycle. The two frozen refs (agent, chat) are rebound via their
        setters — ``_wire_ai`` is deliberately NOT re-run (it double-subscribes
        reflection); the reviewer, reflection, and analysis roles auto-follow
        (they read ``self._backend`` fresh / resolve ``self._utility_backend``
        via a lambda at call time)."""
        if self.agent is None or self.chat is None \
                or self._backend is None or self._utility_backend is None:
            raise ConfigError("AI is not configured")
        agent, chat = self.agent, self.chat
        old_primary: ChatBackend = self._backend
        old_utility: ChatBackend = self._utility_backend

        base = self.config.ai
        backend_changed = backend != base.backend
        base_url = (base.base_url or DEFAULT_LM_STUDIO_URL) \
            if backend == "openai_compatible" else base.base_url
        update: dict[str, object | None] = {
            "backend": backend, "model": model, "base_url": base_url,
        }
        if backend_changed:
            # A stale cross-backend utility id would break the new backend, so
            # utility follows the primary until the operator re-tiers in YAML.
            update["utility_model"] = None
        # Constructing the target runs the AIConfig validator early — a bad
        # base_url / missing credential name surfaces here, before any swap.
        target = base.model_copy(update=update)

        # -- Preconditions: prove, THEN commit. This runs BEFORE build_backends
        #    (which would otherwise raise a raw VaultError from vault.get) and
        #    before any rebind, so a failure leaves the running backend intact.
        if backend == "anthropic":
            if base.api_key_credential not in self.vault.names():
                raise ConfigError(
                    "Set your Anthropic API key in the vault first (Account view / "
                    "poseidon vault set anthropic_api_key) before switching to the "
                    "Claude API."
                )
        else:
            reachable, _ = await probe_local_models(base_url or DEFAULT_LM_STUDIO_URL)
            if not reachable:
                raise ConfigError(
                    f"LM Studio not reachable at {base_url} — start it and load a "
                    "model, then retry."
                )

        paid = backend == "anthropic"
        # -- The swap, guarded so it can never land mid-cycle. Ordering mirrors
        #    ``switch_broker``: PROVE (build) -> PERSIST -> only THEN swap the
        #    in-memory brain. If the persist raises (an unparseable hand-edited
        #    overlay -> ConfigError, or a filesystem fault in ``_save_overlay``
        #    -> OSError: disk full / read-only fs / permissions), the freshly
        #    built but uncommitted backends are closed and a clean error is
        #    raised with the OLD backend still live — so the caller's 422 never
        #    lies about a switch that silently happened (which, into the paid
        #    Claude API, would bill while the UI reported failure). Only after a
        #    durable persist do we rebind, audit, and — past the lock — notify.
        async with self._cycle_lock:
            new_primary, new_utility = build_backends(target, self.vault.get)
            try:
                await asyncio.to_thread(self._write_ai_overlay, target,
                                        clear_utility=backend_changed)
            except Exception as exc:
                # Persist failed: nothing is swapped. Drop the proven-but-
                # uncommitted backends (de-duped for the untiered shared-object
                # case) and surface an actionable error; ConfigError/VaultError
                # keep their message, any other fault (OSError from the atomic
                # write) is wrapped so the route maps it to 422, not a 500.
                closed_fail: set[int] = set()
                for b in (new_primary, new_utility):
                    if id(b) in closed_fail:
                        continue
                    closed_fail.add(id(b))
                    with contextlib.suppress(Exception):
                        await b.aclose()
                if isinstance(exc, ConfigError | VaultError):
                    raise
                raise ConfigError(f"could not persist the AI config: {exc}") from exc
            self._backend = new_primary
            agent.rebind_backend(new_primary)
            self._utility_backend = new_utility
            chat.rebind_backend(new_utility)
            self.config.ai = target
            await self.audit.append("human", "ai.backend_changed",
                                    {"backend": backend, "model": model, "paid": paid})

        # -- Close the displaced backends AFTER releasing the lock (mirrors
        #    shutdown). Skip any object reused as a new backend, and de-dupe the
        #    untiered shared-object case so it is never closed twice.
        closed: set[int] = set()
        for b in (old_primary, old_utility):
            if b is new_primary or b is new_utility or id(b) in closed:
                continue
            closed.add(id(b))
            with contextlib.suppress(Exception):
                await b.aclose()

        await self.bus.publish(Topics.NOTIFY, {
            "level": "warning" if paid else "info",
            "title": f"AI brain: {'Claude API' if paid else 'Local'} · {model}",
            "body": (("Trading decisions now run on the paid Claude API — billed per token."
                      if paid else
                      "Trading decisions now run on the free local model.")
                     + " Switching the AI brain does not change your operating mode."),
        })
        return {"backend": backend, "model": model, "base_url": base_url, "paid": paid}

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
        try:
            result = await self.chat.send(message, context=self._chat_context())
        except AgentError as exc:
            # Meter tokens already billed on earlier tool-loop calls before the
            # failure, so the monthly budget is not silently under-counted.
            await self._record_ai_usage(getattr(exc, "usage", None), "chat")
            raise
        await self._record_ai_usage(result.get("usage"), "chat")
        return result

    async def _record_ai_usage(self, usage: object, prefix: str, *, cycle_id: str | None = None) -> None:
        """Persist an AI usage record. Safe to call with partial usage from a
        failed cycle/chat turn; a zero-call usage writes nothing."""
        if not isinstance(usage, dict) or not usage.get("api_calls"):
            return
        await self.db.execute(
            "INSERT OR REPLACE INTO ai_usage (cycle_id, at, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, api_calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cycle_id or f"{prefix}-{uuid.uuid4().hex[:8]}", datetime.now(UTC).isoformat(),
             usage.get("input_tokens", 0), usage.get("output_tokens", 0),
             usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0),
             usage.get("api_calls", 0)),
        )

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
        self.scheduler.register_job("advisory_prune", self._advisory_prune_job)
        self.scheduler.register_job("position_guardian", self.guardian.check_all)
        self.scheduler.register_job("daily_report", self.send_daily_report)
        self.scheduler.register_job("risk_metrics", self._risk_metrics_job)
        if self.analysis is not None:
            self.scheduler.register_job("analysis_sweep", self.analysis.run_sweep)
        if self.strategy_health is not None:
            self.scheduler.register_job("strategy_health_sweep", self.strategy_health.sweep)

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
        if not any(s.job == "advisory_prune" and s.enabled for s in schedules):
            schedules.append(
                ScheduleConfig(name="nightly-advisory-prune", job="advisory_prune",
                               cron="30 2 * * *")  # after nightly-audit-verify (02:15)
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
        if self.config.ai.analysis.enabled and not any(
            s.job == "analysis_sweep" and s.enabled for s in schedules
        ):
            schedules.append(
                ScheduleConfig(name="default-analysis-sweep", job="analysis_sweep",
                               cron="30 8 * * *")  # daily pre-market (America/New_York)
            )
        if self.config.strategy_health.enabled and not any(
            s.job == "strategy_health_sweep" and s.enabled for s in schedules
        ):
            schedules.append(
                ScheduleConfig(name="default-strategy-health", job="strategy_health_sweep",
                               cron="0 6 * * *")  # daily pre-market
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
                lessons = (await self.reflection.relevant_lessons(
                    self.config.all_watchlist_symbols())
                    if self.reflection is not None else None)
                packets = (await self.analysis.relevant_packets(
                    self.config.all_watchlist_symbols())
                    if self.analysis is not None else [])
                decision = await self.agent.run_cycle(
                    mode=self.order_manager.mode,
                    watchlist=self.config.all_watchlist_symbols(),
                    enabled_strategies=self.strategies.enabled_names,
                    strategy_signals=[s.as_dict() for s in signals],
                    market_session=self.clock.session().value,
                    market_regime=await self._regime_line(),
                    trade_lessons=lessons,
                    analysis_packets=packets,
                )
            except AgentRefusedError as exc:
                log.warning("agent refused; cycle skipped", error=str(exc))
                # Meter tokens already billed for the refusing call before returning
                # (run_cycle records usage before it raises), mirroring the
                # AgentError/DataError branch below so the monthly AI budget is not
                # silently under-counted on repeated refusals.
                await self._record_ai_usage(
                    self.agent.last_cycle_usage(), "refused",
                    cycle_id=f"refused-{uuid.uuid4().hex[:8]}")
                return
            except BackendUnreachableError as exc:
                # Connect-phase failure: the model backend is down, not erroring.
                # Degrade exactly like the generic branch below (meter usage, publish,
                # return — never re-raise) but emit a tailored, actionable hint under a
                # distinct component so the operator knows the fix. Must precede the
                # generic branch because BackendUnreachableError subclasses AgentError.
                log.error("model backend unreachable", error=str(exc))
                await self._record_ai_usage(
                    self.agent.last_cycle_usage(), "failed",
                    cycle_id=f"failed-{uuid.uuid4().hex[:8]}")
                if self.config.ai.backend == "openai_compatible":
                    hint = (f"Model backend unreachable at {self.config.ai.base_url} — "
                            "is LM Studio (or your model server) running?")
                else:
                    hint = "Cannot reach the Anthropic API — check your network."
                await self.bus.publish(Topics.SYSTEM_ERROR,
                                       {"component": "model_backend", "error": hint})
                return
            except (AgentError, DataError) as exc:
                log.error("review cycle failed", error=str(exc))
                # Meter tokens spent on completed sub-calls before the failure.
                await self._record_ai_usage(
                    self.agent.last_cycle_usage(), "failed",
                    cycle_id=f"failed-{uuid.uuid4().hex[:8]}")
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

        if self.agent is None or self._backend is None:
            raise ConfigError("AI agent is not initialized")
        review = await review_algorithm(
            self._backend, source=source, instructions=instructions,
        )
        usage = review.pop("usage", {})
        await self.db.execute(
            "INSERT INTO ai_usage (cycle_id, at, input_tokens, output_tokens, api_calls) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"algo-review-{uuid.uuid4().hex[:8]}", datetime.now(UTC).isoformat(),
             int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
             int(usage.get("api_calls", 1))),
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
        marks = await self.db.fetch_all(
            "SELECT at, equity FROM equity_marks WHERE broker = ? ORDER BY at ASC",
            (self.broker.account_scope,),
        )
        equity_points = [(datetime.fromisoformat(r[0]), float(r[1])) for r in marks]
        trips = build_round_trips(await self._load_all_fills())
        report = compute_performance(equity_points, trips).as_dict()
        report["open_exit_plans"] = await self.guardian.active_plans()
        return report

    async def _load_all_fills(self) -> list[FillRecord]:
        """All filled orders as FillRecords, attributed per strategy. Fills are
        account-scoped like the equity marks: paper round trips must not
        inflate a real account's win rate or per-strategy P&L (and vice
        versa). Pre-migration rows (account_scope='') are excluded, matching
        the equity_marks convention. Shared by the performance report and the
        strategy-health sweep."""
        from decimal import Decimal

        from .core.enums import AssetClass, OrderSide, OrderStatus
        from .core.models import Order

        rows = await self.db.fetch_all(
            "SELECT payload FROM orders WHERE status = ? AND account_scope = ? "
            "ORDER BY updated_at ASC",
            (OrderStatus.FILLED.value, self.broker.account_scope),
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
                multiplier=Decimal(100) if order.asset_class is AssetClass.OPTION else Decimal(1),
            ))
        return fills

    async def _load_strategy_trips(self) -> list[RoundTrip]:
        """All closed round-trips attributed per strategy (reuses the
        performance loader) — feeds the strategy-health sweep."""
        return build_round_trips(await self._load_all_fills())

    async def _retire_strategy(self, strategy: str) -> bool:
        """Reduce-only: deactivate the ACTIVE CUSTOM strategy of this name,
        else no-op. Returns True iff it deactivated one. Never activates or
        orders."""
        try:
            for algo in await self.workshop.list_all():
                if f"algo:{algo.get('name')}" == strategy and algo.get("status") == "active":
                    # Deactivation pops the sleeve cap from the LIVE RiskEngine
                    # dict; mid-cycle that would loosen PositionSizeRule for an
                    # in-flight attributed order. Serialize against the cycle so
                    # the pop lands only between cycles (the sweep never runs
                    # inside a cycle, so this cannot deadlock; a concurrent
                    # cycle tick skipping while we briefly hold the lock is
                    # acceptable).
                    async with self._cycle_lock:
                        await self.workshop.deactivate(algo["id"], archive=False, actor="system")
                    return True
        except Exception as exc:
            log.warning("auto-retire failed", strategy=strategy, error=str(exc))
        return False

    def _symbol_is_flat(self, symbol: str) -> bool:
        p = self.portfolio.position_for(symbol)
        return p is None or p.quantity == 0

    async def _load_reflection_fills(self, symbol: str | None,
                                     since: str | None = None) -> list[FillRecord]:
        """Filled orders (account-scoped) as FillRecords threaded with the
        originating decision_id, for the reflection loop. Symbol filtering is in
        Python — the orders table keys symbol inside the JSON payload. `since`
        (an ISO updated_at) bounds the per-sync sweep so it never reloads the
        whole filled-order history on each ACCOUNT_SYNCED event."""
        from decimal import Decimal

        from .core.enums import AssetClass, OrderSide, OrderStatus
        from .core.models import Order
        sql = "SELECT payload, decision_id FROM orders WHERE status = ? AND account_scope = ?"
        params: tuple[str, ...] = (OrderStatus.FILLED.value, self.broker.account_scope)
        if since:
            sql += " AND updated_at > ?"
            params = (*params, since)
        sql += " ORDER BY updated_at ASC"
        rows = await self.db.fetch_all(sql, params)
        fills: list[FillRecord] = []
        for (payload, decision_id) in rows:
            order = Order.model_validate(json.loads(payload))
            if order.filled_quantity <= 0 or order.avg_fill_price is None:
                continue
            if symbol is not None and order.symbol != symbol:
                continue
            fills.append(FillRecord(
                symbol=order.symbol, side=OrderSide(order.side),
                quantity=Decimal(str(order.filled_quantity)),
                price=Decimal(str(order.avg_fill_price)),
                at=order.updated_at or datetime.now(UTC),
                strategy=order.strategy, decision_id=decision_id or "",
                multiplier=Decimal(100) if order.asset_class is AssetClass.OPTION else Decimal(1)))
        return fills

    async def send_daily_report(self) -> None:
        """End-of-day digest through the notification channels."""
        performance = await self.performance_report()
        usage = await self.ai_usage_summary()
        today = self.clock.now_eastern().date().isoformat()

        def _is_today(created_at: str | None) -> bool:
            # created_at is stored UTC; attribute by its EASTERN date so an
            # after-hours order (UTC date already rolled) lands in the right
            # session's digest instead of the next day's.
            if not created_at:
                return False
            try:
                return datetime.fromisoformat(created_at).astimezone(EASTERN).date().isoformat() == today
            except ValueError:
                return False

        orders_today = [
            o for o in await self.order_manager.recent_orders(200)
            if _is_today(o.get("created_at"))
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
            # Fail safe: open the breaker FIRST (synchronous, cannot fail) so
            # the halt always fires — even when the audit store itself is the
            # corrupt thing and the append below raises. Auditing before the
            # halt would let a DB write failure skip the halt entirely and keep
            # autonomous trading on an untrustworthy chain. Mirrors /api/halt,
            # which also force_opens before it audits.
            self.risk.circuit.force_open(f"audit chain corrupt at seq {bad_seq}")
            # force_open() does not publish CIRCUIT_OPENED (unlike the
            # error-rate auto-trip), so record this halt in the chain directly —
            # best effort: a write failure must not cancel the halt or the alert.
            with contextlib.suppress(Exception):
                await self.audit.append("system", "circuit.opened",
                                        {"reason": "audit chain corrupt", "bad_seq": bad_seq,
                                         "source": "audit_verify"})
            await self.bus.publish(Topics.NOTIFY, {
                "level": "critical", "title": "Audit chain verification failed",
                "body": f"Record {bad_seq} does not verify. Trading halted.",
            })

    async def _advisory_prune_job(self) -> None:
        """Nightly retention sweep for the advisory trade_lessons/analysis_packets
        tables (see Database.prune_advisory) — read-side caches for the
        reflection/analysis loops, not the tamper-evident audit chain, and
        unbounded without this. No self-guard: like _audit_verify_job and the
        other unconditional jobs above, a failure here is caught, logged, and
        reported by Scheduler._execute — it must never crash the scheduler or
        block another job's tick."""
        lessons, packets = await self.db.prune_advisory(
            lesson_lookback_days=self.config.ai.reflection.lookback_days,
            packet_refresh_hours=self.config.ai.analysis.refresh_hours,
            now=datetime.now(UTC),
        )
        log.info("advisory_prune", lessons=lessons, packets=packets)

    # ---------------------------------------------------------------- control

    async def set_mode(self, mode: TradingMode) -> None:
        previous = self.order_manager.mode
        self.order_manager.set_mode(mode)
        await self.audit.append("human", "mode.changed",
                                {"from": previous.value, "to": mode.value})
        log.info("operating mode changed", was=previous.value, now=mode.value)

    def _halt_file(self) -> Path:
        return self.config.data_dir / "HALT"

    async def halt(self, reason: str) -> None:
        """Operator emergency halt (dashboard HALT). Durable three ways so it
        survives a restart, a DB loss, and an unreachable dashboard: the
        in-memory breaker latch, a DB kv marker, and a filesystem HALT sentinel
        the breaker reads directly. With systemd Restart=always a crash/reboot
        in autonomous mode would otherwise silently re-arm trading."""
        self.risk.circuit.force_open(reason)
        with contextlib.suppress(OSError):
            self._halt_file().write_text(reason, encoding="utf-8")
        await self.db.kv_set("circuit.manual_halt", reason)
        await self.audit.append("human", "trading.halted", {"reason": reason})
        log.warning("trading halted by operator", reason=reason)

    async def resume(self) -> None:
        """Clear an operator halt (dashboard Resume): the breaker latch, the DB
        marker, and the filesystem sentinel."""
        self.risk.circuit.force_close()
        with contextlib.suppress(OSError):
            self._halt_file().unlink(missing_ok=True)
        await self.db.kv_set("circuit.manual_halt", "")
        await self.audit.append("human", "trading.resumed", {})
        log.warning("trading resumed by operator")

    async def _restore_manual_halt(self) -> None:
        """Rehydrate an operator HALT that was active before a restart. Called
        from start() alongside seed_orders_today — restart must not silently
        undo the kill switch. (The audit-corrupt auto-halt needs no marker: a
        corrupt chain already refuses startup via verify_chain.)"""
        reason = await self.db.kv_get("circuit.manual_halt")
        if reason:
            self.risk.circuit.force_open(reason)
            await self.audit.append("system", "trading.halt_restored", {"reason": reason})
            log.warning("restored operator trading halt from before restart", reason=reason)

    async def status_report(self) -> dict[str, object]:
        return {
            "version": __version__,
            "mode": self.order_manager.mode.value,
            "cycle_running": self._cycle_lock.locked(),
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

    async def _stop_advisory_services(self) -> None:
        """Drain the advisory background tasks (short grace, then cancel)
        before the backends, router, and DB they write to are closed."""
        for svc in (self.reflection, self.analysis):
            if svc is not None:
                await svc.stop()

    async def stop(self) -> None:
        log.info("shutting down")
        with contextlib.suppress(Exception):
            await self.audit.append("system", "shutdown", {})
        for closer in (
            self.dashboard.stop, self.updates.stop, self.health.stop,
            self.scheduler.stop, self.sync.stop,
            # With the scheduler and sync publisher stopped, drain in-flight
            # reflection/analysis work. The services' _stopped latch also
            # neutralizes any sync handler still in flight when bus.close()
            # drains it below — it must not spawn tasks against the closed
            # backend or advance the fill watermark past unreflected closes.
            self._stop_advisory_services,
            # After the scheduler stops (no new sweeps), let any in-flight
            # guardian exit dispatch finish rather than abandoning it.
            self.guardian.drain,
            # Then cancel order-status pollers (guardian.drain may have just
            # spawned one) before the broker/DB they write to are closed;
            # resume_open_orders() re-attaches pollers on the next boot.
            self.order_manager.stop,
        ):
            with contextlib.suppress(Exception):
                await closer()
        with contextlib.suppress(Exception):
            await self.broker.disconnect()
        with contextlib.suppress(Exception):
            await self.router.close()
        if self._backend is not None:
            with contextlib.suppress(Exception):
                await self._backend.aclose()
        # Close the utility backend only when tiering made it a distinct instance,
        # so a no-tiering run does not double-close the shared backend.
        if self._utility_backend is not None and self._utility_backend is not self._backend:
            with contextlib.suppress(Exception):
                await self._utility_backend.aclose()
        await self.bus.close()
        await self.db.close()
        log.info("shutdown complete")
