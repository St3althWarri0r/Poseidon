"""Autonomous-mode consent expiry — checker + grant plumbing (control-hardening
spec §5, TDD task 7).

An autonomy grant must be able to expire: a durable ``mode.autonomous_expires_at``
kv bound auto-reverts AUTONOMOUS -> APPROVAL, idempotently and restart-safely. The
behaviour under test is deterministic — no LLM anywhere — and every consequential
action (grant, expiry) is hash-chained into the audit log.

Safety invariants pinned here (spec §5.3, §7.7):
  * the checker no-ops when not AUTONOMOUS (line 1, before any kv read) — a second
    call cannot double-revert or double-notify;
  * the revert uses ``order_manager.set_mode`` DIRECTLY, never ``kernel.set_mode`` —
    the kv latch must survive so a crash-restart loop can NEVER re-arm expired
    autonomy;
  * an unparseable bound is treated as EXPIRED (fail-safe: a corrupt bound must not
    grant unbounded autonomy);
  * startup never EXTENDS an existing grant (future kept, past reverted); it only
    stamps a fresh bound when booting AUTONOMOUS with a ttl and no key.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from poseidon.api.server import build_app
from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig
from poseidon.core.enums import TradingMode
from poseidon.core.events import EventBus, Topics
from poseidon.execution.manager import OrderManager
from poseidon.scheduler.scheduler import Scheduler
from poseidon.security.audit import AuditLog
from poseidon.security.vault import Vault
from poseidon.storage.db import Database

_KEY = "mode.autonomous_expires_at"


def _actions(audit_calls: list[tuple[str, str, dict]]) -> list[str]:
    return [action for (_actor, action, _payload) in audit_calls]


def _spy_audit(audit: AuditLog) -> list[tuple[str, str, dict]]:
    calls: list[tuple[str, str, dict]] = []
    real = audit.append

    async def spy(actor, action, payload=None):
        calls.append((actor, action, payload or {}))
        return await real(actor, action, payload)

    audit.append = spy  # type: ignore[method-assign]
    return calls


def _new_manager(db: Database, audit: AuditLog, bus: EventBus,
                 mode: TradingMode) -> OrderManager:
    """A real OrderManager (its mode/set_mode are pure, in-memory) with stubbed
    broker/risk/approvals — the expiry checker never touches them."""
    return OrderManager(MagicMock(), MagicMock(), MagicMock(), db, audit, bus, mode=mode)


@pytest.fixture
async def kctx(tmp_path):
    """A real ApplicationKernel with only the deps the expiry checker + grant
    plumbing touch: real DB (for the kv latch) + AuditLog (hash chain) + EventBus,
    a real OrderManager booting AUTONOMOUS, and a config whose ``risk`` fields a
    test may flip. NOTIFY payloads are captured by wrapping ``bus.publish``."""
    bus = EventBus()
    db = Database(tmp_path / "autonomy.db")
    await db.open()
    audit = AuditLog(db)
    manager = _new_manager(db, audit, bus, TradingMode.AUTONOMOUS)
    config = AppConfig(data_dir=tmp_path)
    kernel = ApplicationKernel(config, Vault(tmp_path / "v.bin"))
    kernel.bus = bus
    kernel.db = db
    kernel.audit = audit
    kernel.order_manager = manager

    notifies: list[dict] = []
    orig_publish = bus.publish

    async def spy_publish(topic, payload=None):
        if topic == Topics.NOTIFY:
            notifies.append(payload or {})
        return await orig_publish(topic, payload)

    bus.publish = spy_publish  # type: ignore[method-assign]
    yield {"kernel": kernel, "manager": manager, "db": db, "audit": audit,
           "bus": bus, "config": config, "notifies": notifies, "tmp_path": tmp_path}
    await bus.close()
    await db.close()


# -- test_expiry_reverts_and_notifies_critical -------------------------------------

async def test_expiry_reverts_and_notifies_critical(kctx) -> None:
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    audit_calls = _spy_audit(kctx["audit"])
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await db.kv_set(_KEY, past)

    reverted = await kernel._check_autonomy_expiry()

    assert reverted is True
    # AUTONOMOUS -> APPROVAL.
    assert manager.mode is TradingMode.APPROVAL
    # The revert did NOT clear the kv latch (a restart must re-observe the expiry).
    assert await db.kv_get(_KEY) == past
    # A system-actor mode.autonomy_expired fact is hash-chained.
    assert any(
        actor == "system" and action == "mode.autonomy_expired"
        for (actor, action, _payload) in audit_calls
    ), audit_calls
    # A single loud critical notification.
    criticals = [n for n in kctx["notifies"] if n.get("level") == "critical"]
    assert len(criticals) == 1
    assert "approval" in (criticals[0].get("body", "") + criticals[0].get("title", "")).lower()


# -- test_already_approval_is_noop_no_duplicate_notify -----------------------------

async def test_already_approval_is_noop_no_duplicate_notify(kctx) -> None:
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    audit_calls = _spy_audit(kctx["audit"])
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await db.kv_set(_KEY, past)

    # First call reverts and notifies exactly once.
    assert await kernel._check_autonomy_expiry() is True
    assert manager.mode is TradingMode.APPROVAL
    assert len(kctx["notifies"]) == 1

    # A SECOND call sees APPROVAL at line 1 and no-ops — no double revert, no
    # duplicate notification, no second audit fact.
    assert await kernel._check_autonomy_expiry() is False
    assert manager.mode is TradingMode.APPROVAL
    assert len(kctx["notifies"]) == 1
    assert _actions(audit_calls).count("mode.autonomy_expired") == 1


# -- test_concurrent_expiry_checks_revert_once -------------------------------------

async def test_concurrent_expiry_checks_revert_once(kctx) -> None:
    """The scheduler job and the cycle-start hook can fire the checker CONCURRENTLY.
    Both pass the top AUTONOMOUS guard and both await ``db.kv_get`` before either
    reverts; without the post-kv_get re-assert both revert and publish a DUPLICATE
    critical NOTIFY. The fix makes the revert + notification land EXACTLY once (mode
    still ends APPROVAL) — the loser observes APPROVAL after its await and bails."""
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    audit_calls = _spy_audit(kctx["audit"])
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await db.kv_set(_KEY, past)

    # Force a real interleave AT the kv_get await: hold BOTH callers inside kv_get
    # — both already past the top guard while mode is still AUTONOMOUS — until both
    # have arrived, then release. This deterministically opens the race window the
    # fix must close, independent of the DB's own scheduling.
    real_kv_get = db.kv_get
    both_inside = asyncio.Semaphore(0)
    arrivals = 0

    async def gated_kv_get(key):
        nonlocal arrivals
        arrivals += 1
        if arrivals >= 2:  # the second caller unblocks both
            both_inside.release()
            both_inside.release()
        await both_inside.acquire()
        return await real_kv_get(key)

    db.kv_get = gated_kv_get  # type: ignore[method-assign]

    results = await asyncio.gather(
        kernel._check_autonomy_expiry(),
        kernel._check_autonomy_expiry(),
    )

    # Exactly one revert, one audit fact, one critical NOTIFY — never two.
    assert manager.mode is TradingMode.APPROVAL
    assert results.count(True) == 1 and results.count(False) == 1
    criticals = [n for n in kctx["notifies"] if n.get("level") == "critical"]
    assert len(criticals) == 1, criticals
    assert _actions(audit_calls).count("mode.autonomy_expired") == 1


# -- test_unparseable_expiry_treated_expired ---------------------------------------

async def test_unparseable_expiry_treated_expired(kctx) -> None:
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    await db.kv_set(_KEY, "not-a-timestamp")

    reverted = await kernel._check_autonomy_expiry()

    # A corrupt bound is fail-safe EXPIRED — it must not grant unbounded autonomy.
    assert reverted is True
    assert manager.mode is TradingMode.APPROVAL
    assert any(n.get("level") == "critical" for n in kctx["notifies"])


# -- test_expiry_idempotent_across_restart -----------------------------------------

async def test_expiry_idempotent_across_restart(kctx) -> None:
    kernel, db, audit, bus = kctx["kernel"], kctx["db"], kctx["audit"], kctx["bus"]
    kernel.config.mode = TradingMode.AUTONOMOUS
    kernel.config.risk.autonomous_ttl_hours = 4  # a ttl exists, but a stale key latches
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await db.kv_set(_KEY, past)

    # --- boot #1: config says AUTONOMOUS, the persisted key is already expired ---
    assert kernel.order_manager.mode is TradingMode.AUTONOMOUS
    await kernel._init_autonomy_expiry()
    assert kernel.order_manager.mode is TradingMode.APPROVAL
    # Startup must NOT re-arm expired autonomy: the stale key latches unchanged.
    assert await db.kv_get(_KEY) == past
    assert len(kctx["notifies"]) == 1

    # --- boot #2: a crash-restart loop re-arms AUTONOMOUS from config; the same
    # stale key is still there -> it must revert AND notify AGAIN (a genuine new
    # boot event), and it must never re-arm expired autonomy. ---
    kernel.order_manager = _new_manager(db, audit, bus, TradingMode.AUTONOMOUS)
    await kernel._init_autonomy_expiry()
    assert kernel.order_manager.mode is TradingMode.APPROVAL
    assert await db.kv_get(_KEY) == past
    assert len(kctx["notifies"]) == 2


# -- test_startup_never_extends_grant ----------------------------------------------

async def test_startup_never_extends_grant(kctx) -> None:
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    kernel.config.mode = TradingMode.AUTONOMOUS
    kernel.config.risk.autonomous_ttl_hours = 1  # a ttl exists…
    future = (datetime.now(UTC) + timedelta(hours=8)).isoformat()  # …but a live grant is present
    await db.kv_set(_KEY, future)

    await kernel._init_autonomy_expiry()

    # The existing FUTURE grant is honored as-is — startup never extends it to
    # now+ttl (that would silently re-arm autonomy).
    assert await db.kv_get(_KEY) == future
    assert manager.mode is TradingMode.AUTONOMOUS
    assert kctx["notifies"] == []


# -- test_startup_stamps_when_absent_and_ttl_set (the complementary _init branch) --

async def test_startup_stamps_when_absent_and_ttl_set(kctx) -> None:
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    kernel.config.mode = TradingMode.AUTONOMOUS
    kernel.config.risk.autonomous_ttl_hours = 6
    assert await db.kv_get(_KEY) is None  # no key yet

    before = datetime.now(UTC)
    await kernel._init_autonomy_expiry()
    after = datetime.now(UTC)

    stamped = await db.kv_get(_KEY)
    assert stamped  # a fresh bound was stamped
    stamped_at = datetime.fromisoformat(stamped)
    assert before + timedelta(hours=6) <= stamped_at <= after + timedelta(hours=6)
    # Still AUTONOMOUS (the fresh bound is in the future) — no revert, no notify.
    assert manager.mode is TradingMode.AUTONOMOUS
    assert kctx["notifies"] == []


# -- test_ttl_stamped_on_grant -----------------------------------------------------

async def test_ttl_stamped_on_grant(kctx) -> None:
    kernel, db = kctx["kernel"], kctx["db"]
    audit_calls = _spy_audit(kctx["audit"])
    kernel.config.risk.autonomous_ttl_hours = 2

    before = datetime.now(UTC)
    await kernel.set_mode(TradingMode.AUTONOMOUS)
    after = datetime.now(UTC)

    stamped = await db.kv_get(_KEY)
    assert stamped, "granting AUTONOMOUS with a ttl must stamp an expiry bound"
    stamped_at = datetime.fromisoformat(stamped)
    assert before + timedelta(hours=2) <= stamped_at <= after + timedelta(hours=2)
    # The grant is a hash-chained consent fact.
    assert any(
        action == "mode.autonomy_granted" and payload.get("expires_at") == stamped
        for (_actor, action, payload) in audit_calls
    ), audit_calls


# -- test_explicit_expiry_overrides_ttl --------------------------------------------

async def test_explicit_expiry_overrides_ttl(kctx) -> None:
    kernel, db = kctx["kernel"], kctx["db"]
    kernel.config.risk.autonomous_ttl_hours = 2  # a default ttl exists…
    explicit = datetime.now(UTC) + timedelta(days=3)  # …but the operator names a bound

    await kernel.set_mode(TradingMode.AUTONOMOUS, expires_at=explicit)

    # The explicit value wins over the config ttl (spec §5.2).
    assert datetime.fromisoformat(await db.kv_get(_KEY)) == explicit


# -- test_set_mode_non_autonomous_clears_grant -------------------------------------

async def test_set_mode_non_autonomous_clears_grant(kctx) -> None:
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    # A grant is live in the kv latch.
    await db.kv_set(_KEY, (datetime.now(UTC) + timedelta(hours=5)).isoformat())

    # Leaving AUTONOMOUS consumes the grant.
    await kernel.set_mode(TradingMode.APPROVAL)

    assert manager.mode is TradingMode.APPROVAL
    # Cleared to "" (the circuit.manual_halt convention) — falsy, so the checker
    # treats it as absent.
    assert not await db.kv_get(_KEY)


# ============================================================================
# TDD task 8 — wiring: scheduler job + cycle-start + /api/mode (spec §5.4).
# ============================================================================

# -- test_job_registered_and_fires_without_ai --------------------------------------

async def test_job_registered_and_fires_without_ai(kctx) -> None:
    """The ``autonomy_expiry`` job is registered unconditionally and scheduled to
    fire every 60 s with ``only_market_hours=False`` (spec §5.4 site 2) — it must
    run overnight, with no AI, so a grant expiring while the market is closed still
    reverts."""
    kernel, manager, db, bus = (kctx["kernel"], kctx["manager"],
                                kctx["db"], kctx["bus"])

    # (1) _register_jobs wires the job. Stub the heavyweight deps it references so
    # the pure registration call runs; capture the registered names.
    registered: list[str] = []
    recorder = MagicMock()
    recorder.register_job = lambda name, job: registered.append(name)
    kernel.scheduler = recorder
    kernel.sync = MagicMock()
    kernel.guardian = MagicMock()
    kernel._register_jobs()
    assert "autonomy_expiry" in registered

    # (2) _effective_schedules registers it unconditionally, every 60 s, NOT
    # market-hours gated (fires overnight).
    scheds = kernel._effective_schedules()
    auto = [s for s in scheds if s.job == "autonomy_expiry"]
    assert len(auto) == 1, scheds
    assert auto[0].only_market_hours is False
    assert auto[0].every_seconds == 60
    assert auto[0].enabled is True

    # (3) firing the job through a real scheduler reverts an expired grant even
    # though there is NO AI wired (agent is None — the checker never touches it).
    assert kernel.agent is None
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await db.kv_set(_KEY, past)
    scheduler = Scheduler(kernel.clock, bus)
    scheduler.register_job("autonomy_expiry", kernel._autonomy_expiry_job)
    await scheduler.trigger_now("autonomy_expiry")
    assert manager.mode is TradingMode.APPROVAL
    assert any(n.get("level") == "critical" for n in kctx["notifies"])


# -- test_cycle_start_checks_expiry ------------------------------------------------

async def test_cycle_start_checks_expiry(kctx) -> None:
    """The expiry checker is the FIRST statement inside ``run_review_cycle``'s
    ``_cycle_lock`` (spec §5.4 site 3), so the cycle prompt sees the reverted
    mode. Proven by expiring the grant, then short-circuiting the rest of the
    cycle (over-budget) — the revert must still have landed."""
    kernel, manager, db = kctx["kernel"], kctx["manager"], kctx["db"]
    kernel.agent = MagicMock()  # non-None so run_review_cycle enters the lock body
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await db.kv_set(_KEY, past)

    # Short-circuit everything after the checker (which runs first): budget-hit
    # returns before any strategy scan or agent call.
    async def over_budget() -> bool:
        return True

    kernel._over_ai_budget = over_budget  # type: ignore[method-assign]

    await kernel.run_review_cycle()

    # The checker ran at the top of the cycle and reverted before the (skipped)
    # AI work — the mode is APPROVAL, not AUTONOMOUS.
    assert manager.mode is TradingMode.APPROVAL


# -- test_api_mode_accepts_expires_in_hours ----------------------------------------

async def test_api_mode_accepts_expires_in_hours(kctx, monkeypatch) -> None:
    """POST /api/mode accepts an ``expires_in_hours`` bound alongside
    ``mode=autonomous`` and threads it into ``set_mode`` → the durable consent
    latch is stamped now+hours (spec §5.2 operator grant)."""
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN_FILE", raising=False)
    kernel, db = kctx["kernel"], kctx["db"]
    app = build_app(kernel)

    before = datetime.now(UTC)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
        r = await c.post("/api/mode",
                         json={"mode": "autonomous", "expires_in_hours": 5})
    after = datetime.now(UTC)

    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "autonomous"
    stamped = await db.kv_get(_KEY)
    assert stamped, "granting AUTONOMOUS with expires_in_hours must stamp a bound"
    stamped_at = datetime.fromisoformat(stamped)
    assert before + timedelta(hours=5) <= stamped_at <= after + timedelta(hours=5)
    assert kernel.order_manager.mode is TradingMode.AUTONOMOUS


# -- test_api_mode_rejects_non_finite_expires_in_hours -----------------------------

@pytest.mark.parametrize("bad", ["inf", "-inf", "nan"])
async def test_api_mode_rejects_non_finite_expires_in_hours(kctx, monkeypatch, bad) -> None:
    """A non-finite ``expires_in_hours`` — ``inf``/``nan``, which ``float()`` accepts
    from a JSON string — must be a clean 422, never a 500: without the isfinite
    guard ``timedelta(hours=inf)`` raises deep in the handler (spec §5.2 input
    validation). The rejected grant must never touch the durable latch."""
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN_FILE", raising=False)
    kernel, db = kctx["kernel"], kctx["db"]
    app = build_app(kernel)

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
        r = await c.post("/api/mode",
                         json={"mode": "autonomous", "expires_in_hours": bad})

    assert r.status_code == 422, r.text
    assert not await db.kv_get(_KEY), "a rejected grant must not stamp the latch"


# -- test_api_mode_accepts_explicit_expires_at (the complementary parse branch) ----

async def test_api_mode_accepts_explicit_expires_at(kctx, monkeypatch) -> None:
    """POST /api/mode also accepts an explicit ISO ``expires_at`` bound, which
    wins over any configured ttl (spec §5.2)."""
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN_FILE", raising=False)
    kernel, db = kctx["kernel"], kctx["db"]
    kernel.config.risk.autonomous_ttl_hours = 2  # a default ttl exists…
    explicit = datetime.now(UTC) + timedelta(days=3)  # …but the operator names one
    app = build_app(kernel)

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
        r = await c.post("/api/mode",
                         json={"mode": "autonomous",
                               "expires_at": explicit.isoformat()})

    assert r.status_code == 200, r.text
    assert datetime.fromisoformat(await db.kv_get(_KEY)) == explicit
