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

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig
from poseidon.core.enums import TradingMode
from poseidon.core.events import EventBus, Topics
from poseidon.execution.manager import OrderManager
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
