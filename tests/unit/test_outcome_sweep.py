"""Decision outcome resolution + behavior sweeps.

Covers: invariant-6 default-OFF no-ops, watermark seeding (pre-feature history
is never graded), executed/vetoed/unfilled/exit-only/hold status semantics,
skip-and-retry vs max_age aging, |alpha| ranking under the lesson cap, budget
and backend gates (markers are deterministic bookkeeping and still commit —
a deliberate divergence from on_account_synced's whole-sweep budget skip),
crash idempotency via deterministic lesson ids, audit-chain purity (ids/
counts only, never prose), the bias-profile singleton, fail-open negatives,
and the kernel default-schedule wiring pin.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.ai.reflection_service import ReflectionService
from poseidon.analytics.performance import FillRecord
from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig, ReflectionConfig
from poseidon.core.enums import OrderSide
from poseidon.core.models import Bar
from poseidon.security.vault import Vault
from poseidon.storage.db import Database

from .backend_fakes import FakeBackend, refusal, text_end

_NOW = datetime.now(UTC)
_SCOPE = "alpaca:paper"


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _bar(symbol: str, days_ago: float, close: str) -> Bar:
    t = _NOW - timedelta(days=days_ago)
    return Bar(symbol=symbol, open=Decimal(close), high=Decimal(close),
               low=Decimal(close), close=Decimal(close), volume=1,
               start=t, end=t, source="fake")


# Decision created 10d ago; bars at 9/8/7d ago; horizon=2 -> entry bar 9d ago
# (first end >= created), exit bar 7d ago: forward 121/100-1 = +21%, benchmark
# 408/400-1 = +2%, alpha = +19%.
_SYMBOL_BARS = [_bar("AAPL", 9, "100"), _bar("AAPL", 8, "110"), _bar("AAPL", 7, "121")]
_SPY_BARS = [_bar("SPY", 9, "400"), _bar("SPY", 8, "404"), _bar("SPY", 7, "408")]


class _Router:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]] | None = None) -> None:
        self.bars_by_symbol = dict(bars_by_symbol or {})
        self.calls: list[str] = []
        self.raise_for: set[str] = set()

    async def bars(self, symbol, *, timeframe="1d", limit=100):
        self.calls.append(symbol)
        if symbol in self.raise_for:
            raise RuntimeError("provider down")
        return self.bars_by_symbol.get(symbol, [])


def _cfg(**outcome_overrides) -> ReflectionConfig:
    outcomes = {"enabled": True, "horizon_trading_days": 2, "max_age_days": 30,
                "max_decisions_per_sweep": 25, "max_lessons_per_sweep": 3,
                "min_abs_alpha": 0.02}
    outcomes.update(outcome_overrides)
    return ReflectionConfig(outcomes=outcomes)


def _service(db, *, backend, router, cfg, fills=(), over_budget=None,
             record_usage=None, account_scope=None):
    audited: list[tuple] = []

    async def _audit(actor, action, payload):
        audited.append((actor, action, payload))

    async def _load(symbol, since=None):
        out = [f for f in fills if symbol is None or f.symbol == symbol]
        if since:
            out = [f for f in out if f.at.isoformat() > since]
        return out

    svc = ReflectionService(
        db=db, router=router, config=cfg, model="fake",
        get_backend=lambda: backend, load_fills=_load, is_flat=lambda s: True,
        audit_append=_audit, record_usage=record_usage, over_budget=over_budget,
        account_scope=account_scope)
    svc.audited = audited  # type: ignore[attr-defined]
    return svc


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def _seed_watermark(db, days_ago: float = 60) -> None:
    await db.kv_set("reflection.outcome_watermark", _iso(days_ago))


async def _insert_decision(db, did: str, *, days_ago: float, action: str = "trade",
                           trades: list | None = None,
                           rationale: dict | None = None,
                           raw_payload: str | None = None) -> None:
    if raw_payload is None:
        payload: dict = {"action": action, "trades": trades or [], "summary": ""}
        if rationale is not None:
            payload["rationale"] = rationale
        raw_payload = json.dumps(payload)
    await db.execute(
        "INSERT INTO decisions (id, cycle_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (did, "c1", action, raw_payload, _iso(days_ago)))


async def _insert_order(db, oid: str, did: str, status: str) -> None:
    await db.execute(
        "INSERT INTO orders (id, client_order_id, broker, payload, status, "
        "decision_id, created_at, updated_at, account_scope) "
        "VALUES (?, ?, 'paper', '{}', ?, ?, ?, ?, '')",
        (oid, f"c-{oid}", status, did, _iso(10), _iso(10)))


async def _resolution(db, did: str):
    row = await db.fetch_one(
        "SELECT resolved_at, resolution FROM decisions WHERE id = ?", (did,))
    assert row is not None
    return row[0], (json.loads(row[1]) if row[1] else None)


async def _lessons(db) -> list[tuple]:
    return await db.fetch_all(
        "SELECT id, symbol, kind, decision_id, realized_return, alpha, holding_days, "
        "lesson, model FROM trade_lessons ORDER BY id")


_BUY = [{"symbol": "AAPL", "side": "buy"}]


# ---- core counterfactual grading --------------------------------------------


async def test_unexecuted_buy_resolves_with_counterfactual_lesson(db) -> None:
    did = "decision-aaaa-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    backend = FakeBackend([text_end("Process was sound; sizing was the miss.")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    resolved_at, res = await _resolution(db, did)
    assert resolved_at is not None and res["status"] == "unexecuted"
    assert res["horizon_trading_days"] == 2
    (out,) = res["outcomes"]
    assert out["symbol"] == "AAPL" and out["side"] == "buy"
    assert abs(out["forward_return"] - 0.21) < 1e-6
    assert out["benchmark"] == "SPY" and abs(out["benchmark_return"] - 0.02) < 1e-6
    assert abs(out["alpha"] - 0.19) < 1e-6

    (lesson,) = await _lessons(db)
    lid, symbol, kind, decision_id, ret, alpha, holding, prose, model = lesson
    assert lid == f"cf-{did[:16]}" and kind == "counterfactual"
    assert symbol == "AAPL" and decision_id == did
    assert abs(ret - 0.21) < 1e-6 and abs(alpha - 0.19) < 1e-6 and holding == 2.0
    assert "sizing was the miss" in prose and model == "fake"

    actions = [a for (_, a, _p) in svc.audited]  # type: ignore[attr-defined]
    assert actions == ["lesson_written", "outcomes_resolved"]
    assert svc.audited[1][2] == {"resolved": 1, "lessons": 1, "pending": 0}  # type: ignore[attr-defined]


async def test_executed_decision_marks_without_grading(db) -> None:
    did = "decision-exec-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    await _insert_order(db, "o1", did, "filled")
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    backend = FakeBackend([])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    assert res == {"status": "executed", "orders": {"filled": 1}}
    assert await _lessons(db) == []
    assert router.calls == []  # the episode loop owns executed grading
    assert backend.calls == []


async def test_risk_vetoed_proposal_is_graded_with_blocked_status(db) -> None:
    # The program's core gap: vetoed calls used to teach nothing.
    did = "decision-veto-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY,
                           rationale={"thesis": "breakout", "confidence": 0.8,
                                      "invalidation": "loses 50dma"})
    await _insert_order(db, "o1", did, "rejected_risk")
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    backend = FakeBackend([text_end("The veto was correct discipline.")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    assert res["status"] == "unexecuted" and res["orders"] == {"rejected_risk": 1}
    sent = backend.calls[0]["messages"][0]["content"]
    assert "vetoed by the risk engine" in sent
    assert "Original thesis: breakout" in sent
    (lesson,) = await _lessons(db)
    assert lesson[0] == f"cf-{did[:16]}"


async def test_human_rejected_and_unfilled_statuses(db) -> None:
    await _seed_watermark(db)
    await _insert_decision(db, "d-human", days_ago=10, trades=_BUY)
    await _insert_order(db, "o1", "d-human", "rejected_human")
    await _insert_decision(db, "d-unfilled", days_ago=10,
                           trades=[{"symbol": "TSLA", "side": "buy"}])
    await _insert_order(db, "o2", "d-unfilled", "canceled")
    tsla = [_bar("TSLA", 9, "100"), _bar("TSLA", 8, "101"), _bar("TSLA", 7, "102")]
    router = _Router({"AAPL": _SYMBOL_BARS, "TSLA": tsla, "SPY": _SPY_BARS})
    backend = FakeBackend([text_end("l1"), text_end("l2")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    sent = " ".join(c["messages"][0]["content"] for c in backend.calls)
    assert "declined by the human" in sent      # d-human graded (alpha 0.19)
    _, res = await _resolution(db, "d-unfilled")  # sub-threshold: |alpha| 0
    assert res["status"] == "unexecuted" and res["orders"] == {"canceled": 1}


async def test_exit_only_proposal_never_graded_as_missed_open(db) -> None:
    did = "decision-exit-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10,
                           trades=[{"symbol": "AAPL", "side": "sell_to_close"}])
    await _insert_order(db, "o1", did, "rejected_risk")
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    backend = FakeBackend([])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    assert res == {"status": "unexecuted_exit_only", "orders": {"rejected_risk": 1}}
    assert await _lessons(db) == [] and router.calls == [] and backend.calls == []


async def test_sell_to_open_counterfactual_inverts_sign(db) -> None:
    did = "decision-shrt-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10,
                           trades=[{"symbol": "AAPL", "side": "sell_to_open"}])
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    backend = FakeBackend([text_end("Shorting into strength failed.")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    (out,) = res["outcomes"]
    assert abs(out["forward_return"] - (-0.21)) < 1e-6  # signed in proposal direction
    assert abs(out["alpha"] - (-0.23)) < 1e-6
    (lesson,) = await _lessons(db)
    assert abs(lesson[4] - (-0.21)) < 1e-6


# ---- hold path --------------------------------------------------------------


async def _equity_mark(db, days_ago: float, equity: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO equity_marks (at, equity, cash, day_pnl, broker) "
        "VALUES (?, ?, '0', NULL, ?)", (_iso(days_ago), equity, _SCOPE))


async def test_hold_decision_graded_portfolio_vs_benchmark(db) -> None:
    did = "decision-hold-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, action="hold", trades=[])
    await _equity_mark(db, 9, "10000")   # at the entry bar end
    await _equity_mark(db, 7, "10500")   # at the exit bar end
    router = _Router({"SPY": _SPY_BARS})
    backend = FakeBackend([text_end("Holding beat the tape; patience earned it.")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg(),
                   account_scope=lambda: _SCOPE)

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    assert res["status"] == "hold" and res["benchmark"] == "SPY"
    assert abs(res["benchmark_return"] - 0.02) < 1e-6
    assert abs(res["portfolio_return"] - 0.05) < 1e-6
    assert abs(res["alpha"] - 0.03) < 1e-6
    (lesson,) = await _lessons(db)
    assert lesson[0] == f"hold-{did[:16]}" and lesson[2] == "hold"
    assert lesson[1] == "PORTFOLIO"
    assert "no trades were proposed" in backend.calls[0]["messages"][0]["content"]


async def test_hold_without_marks_resolves_without_lesson(db) -> None:
    did = "decision-hold-0002"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, action="hold", trades=[])
    router = _Router({"SPY": _SPY_BARS})
    backend = FakeBackend([])
    svc = _service(db, backend=backend, router=router, cfg=_cfg(),
                   account_scope=lambda: _SCOPE)

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    assert res["status"] == "hold" and res["portfolio_return"] is None
    assert res["alpha"] is None
    assert await _lessons(db) == [] and backend.calls == []


async def test_hold_same_mark_for_both_probes_yields_none(db) -> None:
    # One stale mark satisfies both asof probes (app-down window): that must
    # read as "unknown", never as a fake flat book.
    did = "decision-hold-0003"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, action="hold", trades=[])
    await _equity_mark(db, 20, "10000")  # before BOTH probe timestamps
    router = _Router({"SPY": _SPY_BARS})
    svc = _service(db, backend=FakeBackend([]), router=router, cfg=_cfg(),
                   account_scope=lambda: _SCOPE)

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    assert res["portfolio_return"] is None and res["alpha"] is None
    assert await _lessons(db) == []


# ---- invariant 6: disabled == inert -----------------------------------------


async def _db_snapshot(db) -> tuple:
    return (await db.fetch_all("SELECT * FROM decisions ORDER BY id"),
            await db.fetch_all("SELECT * FROM kv ORDER BY key"),
            await db.fetch_all("SELECT * FROM trade_lessons ORDER BY id"))


async def test_default_off_is_total_noop(db) -> None:
    await _insert_decision(db, "d1", days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    backend = FakeBackend([])
    svc = _service(db, backend=backend, router=router, cfg=ReflectionConfig(),
                   fills=_behavior_fills())
    before = await _db_snapshot(db)

    await svc.resolve_outcomes()
    await svc.behavior_sweep()

    assert await _db_snapshot(db) == before  # byte-identical rows
    assert router.calls == [] and backend.calls == []
    assert svc.audited == []  # type: ignore[attr-defined]


def test_default_schedules_only_when_enabled(tmp_path) -> None:
    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    jobs = {s.job for s in kernel._effective_schedules()}
    assert "outcome_sweep" not in jobs and "behavior_sweep" not in jobs
    on = ApplicationKernel(
        AppConfig(ai={"reflection": {"outcomes": {"enabled": True},
                                     "behavior": {"enabled": True}}}),
        Vault(tmp_path / "v2.bin"))
    scheds = on._effective_schedules()
    assert any(s.name == "default-outcome-sweep" and s.job == "outcome_sweep"
               for s in scheds)
    assert any(s.name == "default-behavior-sweep" and s.job == "behavior_sweep"
               for s in scheds)


# ---- watermark --------------------------------------------------------------


async def test_first_enabled_run_seeds_watermark_and_resolves_nothing(db) -> None:
    await _insert_decision(db, "d-pre", days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    svc = _service(db, backend=FakeBackend([]), router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    seeded = await db.kv_get("reflection.outcome_watermark", "")
    assert seeded and seeded >= _NOW.isoformat()
    resolved_at, _ = await _resolution(db, "d-pre")
    assert resolved_at is None and router.calls == []

    # Second sweep: the pre-feature decision sits BEFORE the watermark and is
    # never scanned — mirrors the fill-watermark convention.
    await svc.resolve_outcomes()
    resolved_at, _ = await _resolution(db, "d-pre")
    assert resolved_at is None
    assert svc.audited == []  # type: ignore[attr-defined]


# ---- skip-and-retry + aging -------------------------------------------------


async def test_missing_forward_bars_leave_decision_pending_then_resolve(db) -> None:
    did = "decision-wait-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS[:2], "SPY": _SPY_BARS})  # 1 forward bar only
    backend = FakeBackend([text_end("Lesson.")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()
    resolved_at, _ = await _resolution(db, did)
    assert resolved_at is None                      # PENDING, no marker
    assert await _lessons(db) == []
    assert svc.audited == []  # type: ignore[attr-defined]

    router.bars_by_symbol["AAPL"] = _SYMBOL_BARS    # bars extended: retry succeeds
    await svc.resolve_outcomes()
    resolved_at, res = await _resolution(db, did)
    assert resolved_at is not None and res["status"] == "unexecuted"


async def test_aged_out_decision_becomes_unresolvable(db) -> None:
    did = "decision-aged-0001"
    await _seed_watermark(db, days_ago=60)
    await _insert_decision(db, did, days_ago=40, trades=_BUY)  # > max_age_days=30
    router = _Router({"SPY": _SPY_BARS})  # AAPL bars never arrive
    svc = _service(db, backend=FakeBackend([]), router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    _, res = await _resolution(db, did)
    assert res == {"status": "unresolvable"}
    assert await _lessons(db) == []


# ---- ranking, caps, materiality --------------------------------------------


async def test_lesson_cap_ranks_by_abs_alpha_but_marks_everything(db) -> None:
    await _seed_watermark(db)
    series = {
        "AAA": ("100", "110", "121"),   # fwd +21%, alpha +19%
        "BBB": ("100", "105", "112"),   # fwd +12%, alpha +10%
        "CCC": ("100", "103", "107"),   # fwd +7%,  alpha +5%
        "DDD": ("100", "101", "103"),   # fwd +3%,  alpha +1% (< min_abs_alpha)
    }
    bars = {"SPY": _SPY_BARS}
    for sym, closes in series.items():
        bars[sym] = [_bar(sym, 9, closes[0]), _bar(sym, 8, closes[1]),
                     _bar(sym, 7, closes[2])]
        await _insert_decision(db, f"d-{sym}", days_ago=10,
                               trades=[{"symbol": sym, "side": "buy"}])
    router = _Router(bars)
    backend = FakeBackend([text_end("l1"), text_end("l2"), text_end("l3")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg(max_lessons_per_sweep=2))

    await svc.resolve_outcomes()

    for sym in series:  # ALL FOUR get resolution markers
        resolved_at, _ = await _resolution(db, f"d-{sym}")
        assert resolved_at is not None, sym
    ids = [lesson[0] for lesson in await _lessons(db)]
    assert sorted(ids) == sorted([f"cf-{'d-AAA'[:16]}", f"cf-{'d-BBB'[:16]}"])
    assert len(backend.calls) == 2                 # LLM capped with the lessons
    assert router.calls.count("SPY") == 1          # per-sweep bars cache
    counts = svc.audited[-1][2]  # type: ignore[attr-defined]
    assert counts == {"resolved": 4, "lessons": 2, "pending": 0}


async def test_sub_threshold_alpha_resolves_without_lesson(db) -> None:
    did = "decision-tiny-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10,
                           trades=[{"symbol": "DDD", "side": "buy"}])
    router = _Router({"DDD": [_bar("DDD", 9, "100"), _bar("DDD", 8, "101"),
                              _bar("DDD", 7, "103")], "SPY": _SPY_BARS})
    backend = FakeBackend([])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    resolved_at, _ = await _resolution(db, did)
    assert resolved_at is not None
    assert await _lessons(db) == [] and backend.calls == []


async def test_crash_between_lesson_and_marker_is_idempotent(db) -> None:
    did = "decision-aaaa-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    svc = _service(db, backend=FakeBackend([text_end("v1")]), router=router, cfg=_cfg())
    await svc.resolve_outcomes()
    # Simulate the crash window: lesson landed, marker did not.
    await db.execute("UPDATE decisions SET resolved_at = NULL, resolution = NULL "
                     "WHERE id = ?", (did,))

    retry = _service(db, backend=FakeBackend([text_end("v2")]), router=router,
                     cfg=_cfg())
    await retry.resolve_outcomes()

    lessons = await _lessons(db)
    assert len(lessons) == 1                       # REPLACEd, not duplicated
    assert lessons[0][0] == f"cf-{did[:16]}" and lessons[0][7] == "v2"
    resolved_at, _ = await _resolution(db, did)
    assert resolved_at is not None


# ---- budget / backend gates -------------------------------------------------


async def test_over_budget_still_marks_but_never_calls_llm(db) -> None:
    """The budget gates AI spend, not deterministic bookkeeping — deliberate
    divergence from on_account_synced's whole-sweep budget skip."""
    did = "decision-bdgt-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    backend = FakeBackend([text_end("never sent")])

    async def _over() -> bool:
        return True

    svc = _service(db, backend=backend, router=router, cfg=_cfg(), over_budget=_over)
    await svc.resolve_outcomes()

    resolved_at, _ = await _resolution(db, did)
    assert resolved_at is not None
    assert backend.calls == [] and await _lessons(db) == []


async def test_no_backend_still_marks(db) -> None:
    did = "decision-nobk-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    svc = _service(db, backend=None, router=router, cfg=_cfg())

    await svc.resolve_outcomes()

    resolved_at, _ = await _resolution(db, did)
    assert resolved_at is not None and await _lessons(db) == []


async def test_refusal_marks_resolved_and_meters_usage(db) -> None:
    # A refusing backend must not pin rows pending forever, and refusals bill.
    did = "decision-refu-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    recorded: list[dict] = []

    async def _rec(u):
        recorded.append(u)

    svc = _service(db, backend=FakeBackend([refusal()]), router=router,
                   cfg=_cfg(), record_usage=_rec)
    await svc.resolve_outcomes()

    resolved_at, _ = await _resolution(db, did)
    assert resolved_at is not None
    assert await _lessons(db) == []
    assert recorded and recorded[0].get("input_tokens", 0) >= 1


# ---- audit purity (invariant 4) ---------------------------------------------


async def test_audit_payloads_are_ids_and_counts_only(db) -> None:
    await _seed_watermark(db)
    await _insert_decision(db, "d-a", days_ago=10, trades=_BUY,
                           rationale={"thesis": "secret entry thesis",
                                      "invalidation": "secret tripwire"})
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    prose = "Unique-lesson-prose-marker that must never reach audit."
    both = ReflectionConfig(
        outcomes={"enabled": True, "horizon_trading_days": 2, "max_age_days": 30},
        behavior={"enabled": True, "min_trades": 2})
    svc = _service(db, backend=FakeBackend([text_end(prose)]), router=router,
                   cfg=both, fills=_behavior_fills(),
                   account_scope=lambda: _SCOPE)
    await svc.resolve_outcomes()
    await svc.behavior_sweep()

    payloads = {a: p for (_, a, p) in svc.audited}  # type: ignore[attr-defined]
    assert set(payloads["lesson_written"]) == {"id", "symbol", "kind"}
    assert set(payloads["outcomes_resolved"]) == {"resolved", "lessons", "pending"}
    assert set(payloads["behavior_profile_written"]) == {"id", "trades"}
    flat = json.dumps([p for (_, _, p) in svc.audited])  # type: ignore[attr-defined]
    assert "secret" not in flat and "Unique-lesson-prose-marker" not in flat
    _, res = await _resolution(db, "d-a")
    res_text = json.dumps(res)
    assert "secret" not in res_text and "Unique-lesson-prose-marker" not in res_text


# ---- behavior sweep ---------------------------------------------------------


def _behavior_fills() -> list[FillRecord]:
    def f(sym, side, price, days_ago):
        return FillRecord(symbol=sym, side=side, quantity=Decimal("1"),
                          price=Decimal(price), at=_NOW - timedelta(days=days_ago),
                          strategy="s", decision_id="d")
    return [
        f("AAPL", OrderSide.BUY, "100", 20), f("AAPL", OrderSide.SELL, "110", 18),
        f("MSFT", OrderSide.BUY, "100", 15), f("MSFT", OrderSide.SELL, "90", 13),
    ]


def _behavior_cfg(**overrides) -> ReflectionConfig:
    behavior = {"enabled": True, "min_trades": 2, "window_days": 90,
                "max_bar_symbols": 20}
    behavior.update(overrides)
    return ReflectionConfig(behavior=behavior)


async def test_behavior_sweep_writes_one_deterministic_profile(db) -> None:
    router = _Router()
    backend = FakeBackend([])
    svc = _service(db, backend=backend, router=router, cfg=_behavior_cfg(),
                   fills=_behavior_fills(), account_scope=lambda: _SCOPE)

    await svc.behavior_sweep()
    await svc.behavior_sweep()  # singleton: second run REPLACEs, never appends

    lessons = await _lessons(db)
    assert len(lessons) == 1
    lid, symbol, kind, decision_id, _ret, alpha, holding, prose, model = lessons[0]
    assert lid == f"bias:{_SCOPE}" and kind == "bias_profile"
    assert symbol == "PORTFOLIO" and decision_id is None and alpha is None
    assert holding == 90.0 and model == "deterministic"
    assert prose.endswith("not rules.")
    assert backend.calls == []  # NO LLM ever
    writes = [a for (_, a, _p) in svc.audited]  # type: ignore[attr-defined]
    assert writes == ["behavior_profile_written", "behavior_profile_written"]
    assert svc.audited[0][2] == {"id": f"bias:{_SCOPE}", "trades": 2}  # type: ignore[attr-defined]


async def test_behavior_sweep_silent_below_min_trades(db) -> None:
    svc = _service(db, backend=FakeBackend([]), router=_Router(),
                   cfg=_behavior_cfg(min_trades=10), fills=_behavior_fills())
    await svc.behavior_sweep()
    assert await _lessons(db) == []
    assert svc.audited == []  # type: ignore[attr-defined]


async def test_behavior_bar_fanout_bounded_and_fail_open(db) -> None:
    fills = _behavior_fills() + [
        FillRecord(symbol="ZZZZ", side=OrderSide.BUY, quantity=Decimal("1"),
                   price=Decimal("10"), at=_NOW - timedelta(days=9),
                   strategy="s", decision_id="d"),
        FillRecord(symbol="ZZZZ", side=OrderSide.SELL, quantity=Decimal("1"),
                   price=Decimal("11"), at=_NOW - timedelta(days=8),
                   strategy="s", decision_id="d"),
    ]
    router = _Router()
    svc = _service(db, backend=FakeBackend([]), router=router,
                   cfg=_behavior_cfg(max_bar_symbols=1), fills=fills)
    await svc.behavior_sweep()
    assert len(router.calls) == 1              # fan-out bound (ties alphabetical)
    assert router.calls == ["AAPL"]
    assert len(await _lessons(db)) == 1

    # A raising bars call only loses runup coverage, never the profile.
    await db.execute("DELETE FROM trade_lessons")
    router2 = _Router()
    router2.raise_for.add("AAPL")
    svc2 = _service(db, backend=FakeBackend([]), router=router2,
                    cfg=_behavior_cfg(), fills=fills)
    await svc2.behavior_sweep()
    assert len(await _lessons(db)) == 1


# ---- fail-open negatives ----------------------------------------------------


async def test_raising_router_and_junk_payload_never_propagate(db) -> None:
    await _seed_watermark(db)
    await _insert_decision(db, "d-junk", days_ago=11, raw_payload="not-json{{")
    await _insert_decision(db, "d-good", days_ago=10, trades=_BUY)
    await _insert_decision(db, "d-down", days_ago=10,
                           trades=[{"symbol": "DOWN", "side": "buy"}])
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    router.raise_for.add("DOWN")
    backend = FakeBackend([text_end("Lesson.")])
    svc = _service(db, backend=backend, router=router, cfg=_cfg())

    await svc.resolve_outcomes()  # must not raise

    resolved_at, res = await _resolution(db, "d-good")   # sweep continued
    assert resolved_at is not None and res["status"] == "unexecuted"
    junk_resolved, _ = await _resolution(db, "d-junk")   # stays PENDING
    assert junk_resolved is None
    down_resolved, _ = await _resolution(db, "d-down")   # provider down: PENDING
    assert down_resolved is None
    counts = svc.audited[-1][2]  # type: ignore[attr-defined]
    assert counts == {"resolved": 1, "lessons": 1, "pending": 2}


async def test_raising_backend_marks_resolved_without_lesson(db) -> None:
    class Boom:
        model = "boom"
        async def complete(self, *a, **k):
            raise RuntimeError("down")
        def tool_result_messages(self, results):
            return []
        async def aclose(self):
            return None

    did = "decision-boom-0001"
    await _seed_watermark(db)
    await _insert_decision(db, did, days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    svc = _service(db, backend=Boom(), router=router, cfg=_cfg())

    await svc.resolve_outcomes()  # must not raise

    resolved_at, _ = await _resolution(db, did)
    assert resolved_at is not None and await _lessons(db) == []


async def test_stopped_service_returns_immediately(db) -> None:
    await _seed_watermark(db)
    await _insert_decision(db, "d1", days_ago=10, trades=_BUY)
    router = _Router({"AAPL": _SYMBOL_BARS, "SPY": _SPY_BARS})
    svc = _service(db, backend=FakeBackend([]), router=router, cfg=_cfg(),
                   fills=_behavior_fills())
    svc._stopped = True

    await svc.resolve_outcomes()
    await svc.behavior_sweep()

    resolved_at, _ = await _resolution(db, "d1")
    assert resolved_at is None and router.calls == []
    assert await _lessons(db) == []
