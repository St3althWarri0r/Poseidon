"""Regression tests for the adversarial review pass.

Each test pins a specific confirmed finding so the fix cannot silently
regress. Grouped by subsystem; see the commit message for the finding list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poseidon.analytics.performance import FillRecord, build_round_trips
from poseidon.core.config import _deep_env_overrides, dashboard_token_from_env
from poseidon.core.enums import AssetClass, OrderSide, OrderStatus, OrderType
from poseidon.core.errors import RiskViolation
from poseidon.core.models import AccountSnapshot, OptionLeg, Order, Position
from poseidon.data.base import MarketDataProvider
from poseidon.portfolio.state import PortfolioState
from poseidon.risk.rules import ReduceOnlyRule
from poseidon.strategy.custom import CustomAlgorithm, _safe_builtins, validate_algorithm

from ..conftest import make_quote

NOW = datetime.now(UTC)


# ---------------------------------------------------------------- RCE sandbox (finding 1)

RCE_PAYLOADS = [
    "async def scan(ctx):\n    __builtins__['__import__']('os').system('id')\n    return []",
    "async def scan(ctx):\n    ([open][0])('/tmp/pwn','w')\n    return []",
    "async def scan(ctx):\n    open('/tmp/pwn','w')\n    return []",
]


@pytest.mark.parametrize("src", RCE_PAYLOADS)
def test_algorithm_sandbox_blocks_exploits(src: str) -> None:
    # Static screen rejects every known bypass...
    assert validate_algorithm(src)
    # ...and construction (exec) never succeeds for them either.
    with pytest.raises(ValueError):
        CustomAlgorithm(algo_name="x", source=src, symbols=["AAPL"])


def test_algorithm_sandbox_has_no_dangerous_builtins() -> None:
    safe = _safe_builtins()
    for name in ("open", "exec", "eval", "compile", "__import__", "getattr", "globals"):
        assert safe.get(name) is not (globals().get("__builtins__") or {})  # not the real one
    assert "open" not in safe and "eval" not in safe


def test_legit_algorithm_still_runs() -> None:
    src = ("async def scan(ctx):\n"
           "    from datetime import datetime\n"
           "    vals = sorted([3.0, 1.0, 2.0])\n"
           "    return [{'symbol': 'AAPL', 'direction': 'long', "
           "'strength': min(round(sum(vals)/len(vals), 2), 1.0)}]\n")
    assert validate_algorithm(src) == []
    CustomAlgorithm(algo_name="legit", source=src, symbols=["AAPL"])  # no raise


# ---------------------------------------------------- naked-short guard (finding 2)

def _portfolio(equity: str = "100000") -> PortfolioState:
    state = PortfolioState()
    state.account = AccountSnapshot(broker="paper", account_id="t", equity=Decimal(equity),
                                    cash=Decimal(equity), buying_power=Decimal(equity), as_of=NOW)
    state.synced_at = NOW
    return state


def _reduce_ctx(order: Order, portfolio: PortfolioState):
    from poseidon.core.clock import MarketClock
    from poseidon.risk.rules import RiskContext
    return RiskContext(order=order, quote=make_quote(order.symbol, "100"),
                       portfolio=portfolio, config=__import__("poseidon.core.config",
                       fromlist=["RiskConfig"]).RiskConfig(), clock=MarketClock())


def test_reduce_only_blocks_naked_short() -> None:
    order = Order(symbol="XYZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                  quantity=Decimal("200"), limit_price=Decimal("100"))
    with pytest.raises(RiskViolation, match="reduce_only"):
        ReduceOnlyRule().check(_reduce_ctx(order, _portfolio()))


def test_reduce_only_allows_closing_a_held_long() -> None:
    state = _portfolio()
    state.positions = [Position(symbol="XYZ", quantity=Decimal("200"),
                                avg_entry_price=Decimal("90"), as_of=NOW)]
    order = Order(symbol="XYZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                  quantity=Decimal("200"), limit_price=Decimal("100"))
    ReduceOnlyRule().check(_reduce_ctx(order, state))  # no raise


def test_reduce_only_rejects_selling_more_than_held() -> None:
    state = _portfolio()
    state.positions = [Position(symbol="XYZ", quantity=Decimal("100"),
                                avg_entry_price=Decimal("90"), as_of=NOW)]
    order = Order(symbol="XYZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                  quantity=Decimal("150"), limit_price=Decimal("100"))
    with pytest.raises(RiskViolation, match="reduce_only"):
        ReduceOnlyRule().check(_reduce_ctx(order, state))


# --------- multi-leg reduce-only must subtract pending closes like single-leg ----------
# The single-leg path subtracts already-pending same-direction closing orders so
# two exits cannot each pass alone and oversell the book into a short. The
# multi-leg branch must apply the identical per-contract accounting, or two
# concurrent option-spread exits on the same contracts oversell into a short.

_CONTRACT = "AAPL240621C00190000"


def _long_option(qty: str) -> Position:
    return Position(symbol=_CONTRACT, asset_class=AssetClass.OPTION,
                    quantity=Decimal(qty), avg_entry_price=Decimal("5"), as_of=NOW)


def _resting(side: OrderSide, qty: str, *, filled: str = "0") -> Order:
    # Broker open-order snapshots normalize *_to_close sides to plain buy/sell.
    return Order(symbol=_CONTRACT, asset_class=AssetClass.OPTION, side=side,
                 order_type=OrderType.LIMIT, quantity=Decimal(qty),
                 filled_quantity=Decimal(filled), limit_price=Decimal("1"),
                 status=OrderStatus.ACCEPTED)


def _multileg_close(leg_side: OrderSide, leg_qty: int, spreads: str) -> Order:
    return Order(symbol="AAPL", side=OrderSide.SELL_TO_CLOSE, order_type=OrderType.LIMIT,
                 quantity=Decimal(spreads), limit_price=Decimal("1"),
                 legs=[OptionLeg(contract_symbol=_CONTRACT, side=leg_side, quantity=leg_qty)])


def test_reduce_only_multileg_closes_full_position_when_nothing_pending() -> None:
    state = _portfolio()
    state.positions = [_long_option("10")]
    # SELL_TO_CLOSE 1 contract x 10 spreads == the whole 10-lot position.
    ReduceOnlyRule().check(_reduce_ctx(_multileg_close(OrderSide.SELL_TO_CLOSE, 1, "10"), state))


def test_reduce_only_multileg_blocks_oversell_against_pending_close() -> None:
    state = _portfolio()
    state.positions = [_long_option("10")]
    # A prior spread exit for the full 10 is already resting at the broker.
    state.open_orders = [_resting(OrderSide.SELL, "10")]
    # A second identical exit would close 10 more -> oversell into a -10 short.
    with pytest.raises(RiskViolation, match="reduce_only"):
        ReduceOnlyRule().check(_reduce_ctx(_multileg_close(OrderSide.SELL_TO_CLOSE, 1, "10"), state))


def test_reduce_only_multileg_subtracts_partial_pending() -> None:
    state = _portfolio()
    state.positions = [_long_option("10")]
    # 6 already pending (a partially-filled resting exit leaves 6 working).
    state.open_orders = [_resting(OrderSide.SELL, "10", filled="4")]
    # 5 more would exceed the 4 still closable (10 held - 6 pending); reject.
    with pytest.raises(RiskViolation, match="reduce_only"):
        ReduceOnlyRule().check(_reduce_ctx(_multileg_close(OrderSide.SELL_TO_CLOSE, 1, "5"), state))
    # Exactly the 4 still closable is fine.
    ReduceOnlyRule().check(_reduce_ctx(_multileg_close(OrderSide.SELL_TO_CLOSE, 1, "4"), state))


def test_reduce_only_multileg_pending_is_direction_matched() -> None:
    # A resting BUY on the same contract must not consume a SELL_TO_CLOSE's
    # closable quantity (mirrors the single-leg is_buy match).
    state = _portfolio()
    state.positions = [_long_option("10")]
    state.open_orders = [_resting(OrderSide.BUY, "10")]  # opposite direction
    ReduceOnlyRule().check(_reduce_ctx(_multileg_close(OrderSide.SELL_TO_CLOSE, 1, "10"), state))


def test_reduce_only_multileg_buy_to_close_covers_short_with_pending() -> None:
    # Short 10 contracts (opened via a SELL_TO_OPEN leg); a resting BUY_TO_CLOSE
    # for 10 is pending. A second cover for 10 would flip to a +10 long.
    state = _portfolio()
    state.positions = [_long_option("-10")]
    state.open_orders = [_resting(OrderSide.BUY, "10")]
    with pytest.raises(RiskViolation, match="reduce_only"):
        ReduceOnlyRule().check(_reduce_ctx(_multileg_close(OrderSide.BUY_TO_CLOSE, 1, "10"), state))


# ---------------------------------------------------- loss-halt latch (finding 5)

def test_loss_halt_latches_through_intraday_recovery() -> None:
    state = PortfolioState()
    state.day_start_equity = Decimal("100000")
    state.record_equity(Decimal("96500"), NOW)  # -3.5%
    # Recover to -2.5%; the latch must still report the trough loss.
    state.account = AccountSnapshot(broker="p", account_id="t", equity=Decimal("97500"),
                                    cash=Decimal("97500"), buying_power=Decimal("97500"), as_of=NOW)
    state.record_equity(Decimal("97500"), NOW)
    assert state.day_loss_pct() >= 0.035 - 1e-9


# ---------------------------------------------- option exposure multiplier (finding 10)

def test_gross_exposure_applies_option_multiplier_when_marketless() -> None:
    state = PortfolioState()
    state.positions = [Position(symbol="SPY241220P00500000", asset_class=AssetClass.OPTION,
                                quantity=Decimal("10"), avg_entry_price=Decimal("5"),
                                market_value=None, as_of=NOW)]
    # 10 contracts * $5 premium * 100 multiplier = $5,000, not $50.
    assert state.gross_exposure() == Decimal("5000")


# ------------------------------------------------------- config env (findings 11, 12)

def test_reserved_vault_env_excluded_from_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSEIDON_VAULT_PASSPHRASE_FILE", "/run/secrets/x")
    monkeypatch.setenv("POSEIDON_DASHBOARD_TOKEN", "abc")
    overrides = _deep_env_overrides()
    assert "vault_passphrase_file" not in overrides
    assert "dashboard_token" not in overrides
    assert dashboard_token_from_env() == "abc"


# --------------------------------------------------------- retry-after (finding 15)

def test_retry_after_parses_http_date_and_seconds() -> None:
    assert MarketDataProvider._parse_retry_after("120") == 120.0
    assert MarketDataProvider._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") is not None
    assert MarketDataProvider._parse_retry_after("garbage") is None
    assert MarketDataProvider._parse_retry_after(None) is None


# ----------------------------------------------------- FIFO short round trips (finding 25)

def test_short_round_trip_pnl_is_correct() -> None:
    fills = [
        FillRecord(symbol="X", side=OrderSide.SELL_TO_OPEN, quantity=Decimal("100"),
                   price=Decimal("50"), at=NOW),
        FillRecord(symbol="X", side=OrderSide.BUY_TO_CLOSE, quantity=Decimal("100"),
                   price=Decimal("45"), at=NOW + timedelta(days=1)),
    ]
    trips = build_round_trips(fills)
    assert len(trips) == 1
    # Short sold at 50, covered at 45 -> +$500 profit (not dropped, not a phantom long).
    assert trips[0].is_short and trips[0].pnl == Decimal("500")


# --------------------------------------------------- paper broker stop orders (finding 3)

class _MutableQuoteFn:
    def __init__(self, symbol: str, price: str) -> None:
        self._q = make_quote(symbol, price)

    def set(self, symbol: str, price: str) -> None:
        self._q = make_quote(symbol, price)

    async def __call__(self, _symbol: str):
        return self._q


async def test_paper_stop_order_rests_until_triggered() -> None:
    from poseidon.brokers.plugins.paper import PaperBroker
    from poseidon.core.enums import OrderStatus

    qf = _MutableQuoteFn("AAPL", "190")
    broker = PaperBroker(credentials={}, options={"quote_fn": qf, "starting_cash": "1000000"})
    await broker.connect()

    buy = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=Decimal("100"))
    await broker.submit_order(buy)
    assert buy.status is OrderStatus.FILLED

    # Sell stop at 170 while the market is at ~190: must NOT fill yet.
    stop = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP,
                 quantity=Decimal("100"), stop_price=Decimal("170"))
    await broker.submit_order(stop)
    assert stop.status is not OrderStatus.FILLED

    # Market falls through the stop: now it triggers on the next status poll.
    qf.set("AAPL", "165")
    await broker.order_status(stop)
    assert stop.status is OrderStatus.FILLED


# ------------- audit-verify halt must fire even if the audit write fails -------------
# _audit_verify_job runs when verify_chain() reports the chain corrupt. If it
# appends to the (possibly-corrupt) audit store BEFORE opening the breaker, a DB
# write failure there skips the halt entirely and autonomous trading continues on
# an untrustworthy chain. The kill switch must open first (like /api/halt).

async def test_audit_verify_halts_even_when_the_audit_append_fails(tmp_path) -> None:
    from types import SimpleNamespace

    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AppConfig
    from poseidon.core.events import Topics
    from poseidon.risk.circuit import CircuitBreaker
    from poseidon.security.vault import Vault

    kernel = ApplicationKernel(AppConfig(), Vault(tmp_path / "v.bin"))
    circuit = CircuitBreaker(error_threshold=5, window_seconds=300, cooldown_seconds=1800)
    kernel.risk = SimpleNamespace(circuit=circuit)  # type: ignore[assignment]

    class _CorruptAudit:
        async def verify_chain(self) -> tuple[bool, int | None]:
            return (False, 7)

        async def append(self, *a: object, **k: object) -> None:
            raise RuntimeError("audit DB write failed (corrupt store)")

    kernel.audit = _CorruptAudit()  # type: ignore[assignment]
    notes: list[dict[str, object]] = []

    class _Bus:
        async def publish(self, topic: str, payload: object = None) -> None:
            if topic == Topics.NOTIFY and isinstance(payload, dict):
                notes.append(payload)

    kernel.bus = _Bus()  # type: ignore[assignment]

    # Must NOT propagate the append failure, and the halt MUST still fire.
    await kernel._audit_verify_job()
    assert circuit.is_open
    assert any(n.get("level") == "critical" for n in notes)


# ------------- operator HALT must survive a restart (kill switch persistence) -------------
# force_open() lives only in process memory. With mode: autonomous and systemd
# Restart=always, a crash/reboot after the operator hits HALT would silently
# re-arm live trading. The halt is persisted and rehydrated at startup.

async def test_manual_halt_persists_across_restart(tmp_path) -> None:
    from types import SimpleNamespace

    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AppConfig
    from poseidon.risk.circuit import CircuitBreaker
    from poseidon.security.audit import AuditLog
    from poseidon.security.vault import Vault
    from poseidon.storage.db import Database

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    halt_file = data_dir / "HALT"

    def _fresh_circuit() -> CircuitBreaker:
        return CircuitBreaker(error_threshold=5, window_seconds=300,
                              cooldown_seconds=1800, halt_file=halt_file)

    kernel = ApplicationKernel(AppConfig(data_dir=data_dir), Vault(tmp_path / "v.bin"))
    db = Database(tmp_path / "t.db")
    await db.open()
    kernel.db = db
    kernel.audit = AuditLog(db)
    kernel.risk = SimpleNamespace(circuit=_fresh_circuit())  # type: ignore[assignment]

    await kernel.halt("operator hit HALT")
    assert kernel.risk.circuit.is_open
    assert await db.kv_get("circuit.manual_halt") == "operator hit HALT"
    assert halt_file.exists()  # filesystem sentinel written too

    # Restart: a brand-new breaker. The filesystem sentinel alone re-arms it
    # immediately (before any DB rehydration runs), and the DB path does too.
    kernel.risk = SimpleNamespace(circuit=_fresh_circuit())  # type: ignore[assignment]
    assert kernel.risk.circuit.is_open
    await kernel._restore_manual_halt()
    assert kernel.risk.circuit.is_open

    # Resume clears all three; a restart after resume stays closed.
    await kernel.resume()
    assert not halt_file.exists()
    assert not await db.kv_get("circuit.manual_halt")
    kernel.risk = SimpleNamespace(circuit=_fresh_circuit())  # type: ignore[assignment]
    assert not kernel.risk.circuit.is_open
    await kernel._restore_manual_halt()
    assert not kernel.risk.circuit.is_open

    await db.close()


# ------------------- format-string traversal via runtime assembly (F023) -------------------
# The literal-constant screen in validate_algorithm only inspected whole ast.Constant
# strings, so a '{0.__class__.__init__.__globals__}' template ASSEMBLED at runtime
# (chr()/concatenation) evaded it and could still reach module globals via .format(ctx)
# with no dunder attribute node, no '__builtins__' name, and no subscript call in the AST.
# The guard now also flags .format/.format_map called on a NON-literal template.

_FORMAT_EVASIONS = [
    # chr(46) == '.', so no single string literal ever contains the dunder walk.
    ("async def scan(ctx):\n"
     "    d = chr(46)\n"
     "    t = '{0' + d + '__class__' + d + '__init__' + d + '__globals__}'\n"
     "    t.format(ctx)\n"
     "    return []\n"),
    # plain concatenation of individually-benign literals into a traversal template.
    ("async def scan(ctx):\n"
     "    t = '{0' + '.' + '__class__}'\n"
     "    return [t.format(ctx)]\n"),
    # .format_map on a name-bound, runtime-built template.
    ("async def scan(ctx):\n"
     "    tmpl = chr(123) + '0.__class__' + chr(125)\n"
     "    tmpl.format_map({})\n"
     "    return []\n"),
]


@pytest.mark.parametrize("src", _FORMAT_EVASIONS)
def test_algorithm_blocks_runtime_assembled_format_traversal(src: str) -> None:
    # Pre-fix these return [] (the literal screen never sees an assembled template),
    # so validation passes and CustomAlgorithm constructs — the read/exfil path is live.
    assert validate_algorithm(src), "runtime-assembled .format/.format_map must be flagged"
    with pytest.raises(ValueError):
        CustomAlgorithm(algo_name="fmt", source=src, symbols=["AAPL"])


def test_legit_literal_format_is_still_allowed() -> None:
    # .format on a plain string LITERAL is benign (a literal that actually traverses is
    # still caught by the existing constant screen); only non-literal templates are the
    # new residual, so a normal formatted label must not be flagged.
    src = ("async def scan(ctx):\n"
           "    label = '{:.2f}'.format(1.23456)\n"
           "    return [{'symbol': 'AAPL', 'direction': 'long', 'strength': 0.5,\n"
           "             'evidence': {'label': label}}]\n")
    assert validate_algorithm(src) == []
    CustomAlgorithm(algo_name="ok", source=src, symbols=["AAPL"])  # no raise


def test_bundled_example_algorithms_still_validate() -> None:
    # Acceptance guard: the new .format guard must not false-positive on any shipped
    # starter algorithm (they use f-strings / no .format), or first-boot seeding breaks.
    from pathlib import Path

    import poseidon

    root = Path(poseidon.__file__).resolve().parent
    algo_dir = root / "examples" / "algorithms"
    if not algo_dir.is_dir():
        algo_dir = root.parents[1] / "examples" / "algorithms"
    files = sorted(algo_dir.glob("*.py"))
    assert files, "no bundled example algorithms found to validate"
    for f in files:
        assert validate_algorithm(f.read_text()) == [], f"bundled example {f.name} must stay valid"


# ------------------- dashboard token in browser argv warning (F019) -------------------
# The pywebview window loads the URL in-process, but both browser fallbacks put the
# ?token= in the child's argv (/proc/<pid>/cmdline, world-readable). open_window now
# warns when a token rides the fallback — and stays silent when there is no token.

def test_open_window_warns_when_token_rides_browser_argv(monkeypatch, capsys) -> None:
    import sys

    from poseidon import gui

    monkeypatch.setitem(sys.modules, "webview", None)  # force the browser fallback path
    launched: list[object] = []
    monkeypatch.setattr(gui.shutil, "which", lambda name: "/usr/bin/fakebrowser")
    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **k: launched.append(a[0]))

    assert gui.open_window("http://127.0.0.1:8321/?token=SECRET", token_in_url=True) == 0
    warned = capsys.readouterr().out
    assert launched, "a browser process was launched (fallback taken)"
    assert "/proc" in warned and "token" in warned.lower()

    # No token in the URL -> no leak, so no warning.
    assert gui.open_window("http://127.0.0.1:8321/", token_in_url=False) == 0
    assert "/proc" not in capsys.readouterr().out


# ------------------- refused review cycle still meters AI usage (F001/F003) -------------------
# A review cycle that ends in AgentRefusedError has already billed Anthropic tokens
# (run_cycle records usage before it raises). The handler must meter them like the
# AgentError/DataError sibling, or the monthly budget silently under-counts and cycles
# keep running past the ceiling.

async def test_refused_cycle_still_meters_ai_usage(tmp_path) -> None:
    from types import SimpleNamespace

    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AppConfig
    from poseidon.core.enums import TradingMode
    from poseidon.core.errors import AgentRefusedError
    from poseidon.security.vault import Vault
    from poseidon.storage.db import Database

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    kernel = ApplicationKernel(AppConfig(data_dir=data_dir), Vault(tmp_path / "v.bin"))
    db = Database(tmp_path / "t.db")
    await db.open()
    kernel.db = db

    class _RefusingAgent:
        async def run_cycle(self, **kw: object) -> object:
            raise AgentRefusedError("model declined")

        def last_cycle_usage(self) -> dict[str, int]:
            return {"input_tokens": 1234, "output_tokens": 56, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "api_calls": 3}

    class _NoStrategies:
        enabled_names: list[str] = []

        async def scan_all(self, router: object, portfolio: object) -> list[object]:
            return []

    kernel.agent = _RefusingAgent()  # type: ignore[assignment]
    kernel.strategies = _NoStrategies()  # type: ignore[assignment]
    kernel.risk = SimpleNamespace(set_cycle_attribution=lambda *_: None)  # type: ignore[assignment]
    kernel.order_manager = SimpleNamespace(mode=TradingMode.RESEARCH)  # type: ignore[assignment]
    kernel.router = None  # type: ignore[assignment]

    async def _not_over() -> bool:
        return False

    async def _no_regime() -> None:
        return None

    kernel._over_ai_budget = _not_over  # type: ignore[method-assign]
    kernel._regime_line = _no_regime  # type: ignore[method-assign]

    await kernel.run_review_cycle()

    rows = await db.fetch_all("SELECT cycle_id, input_tokens, api_calls FROM ai_usage")
    assert len(rows) == 1, "a refused cycle must record exactly one ai_usage row"
    assert str(rows[0][0]).startswith("refused-")
    assert rows[0][1] == 1234 and rows[0][2] == 3  # the real billed tokens, not zero
    await db.close()


# ---------- backend-unreachable review cycle: tailored notification (Task 4) ----------
# When the model backend is unreachable (connect-phase failure), run_review_cycle must
# degrade exactly like the generic AgentError branch — meter usage, publish a system.error,
# return cleanly (never re-raise) — BUT publish a tailored, actionable hint under
# component="model_backend" (distinct from the generic "review_cycle" component), so the
# operator sees "is LM Studio running?" instead of the raw connect string. Because
# BackendUnreachableError subclasses AgentError, the new branch must precede the generic
# one or the tailored notification is never emitted.


async def _build_cycle_kernel(tmp_path, agent, *, ai=None):
    """Minimal ApplicationKernel wired just enough to drive run_review_cycle."""
    from types import SimpleNamespace

    from poseidon.app import ApplicationKernel
    from poseidon.core.config import AppConfig
    from poseidon.core.enums import TradingMode
    from poseidon.security.vault import Vault
    from poseidon.storage.db import Database

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = AppConfig(data_dir=data_dir) if ai is None else AppConfig(data_dir=data_dir, ai=ai)
    kernel = ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))
    db = Database(tmp_path / "t.db")
    await db.open()
    kernel.db = db

    class _NoStrategies:
        enabled_names: list[str] = []

        async def scan_all(self, router: object, portfolio: object) -> list[object]:
            return []

    async def _not_over() -> bool:
        return False

    async def _no_regime() -> None:
        return None

    kernel.agent = agent  # type: ignore[assignment]
    kernel.strategies = _NoStrategies()  # type: ignore[assignment]
    kernel.risk = SimpleNamespace(set_cycle_attribution=lambda *_: None)  # type: ignore[assignment]
    kernel.order_manager = SimpleNamespace(mode=TradingMode.RESEARCH)  # type: ignore[assignment]
    kernel.router = None  # type: ignore[assignment]
    kernel._over_ai_budget = _not_over  # type: ignore[method-assign]
    kernel._regime_line = _no_regime  # type: ignore[method-assign]
    return kernel, db


def _stub_agent(exc: Exception):
    class _Agent:
        async def run_cycle(self, **kw: object) -> object:
            raise exc

        def last_cycle_usage(self) -> dict[str, int]:
            return {"input_tokens": 10, "output_tokens": 0, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "api_calls": 1}

    return _Agent()


async def _collect_system_errors(kernel):
    import asyncio

    from poseidon.core.events import Topics

    errors: list[object] = []

    async def _capture(topic: str, payload: object) -> None:
        errors.append(payload)

    kernel.bus.subscribe(Topics.SYSTEM_ERROR, _capture)
    await kernel.run_review_cycle()  # must return cleanly, never re-raise
    for _ in range(3):  # let the fire-and-forget publish task(s) run to completion
        await asyncio.sleep(0)
    return errors


async def test_backend_unreachable_cycle_notifies_model_backend(tmp_path) -> None:
    from poseidon.core.config import AIConfig
    from poseidon.core.errors import BackendUnreachableError

    base = "http://127.0.0.1:1234/v1"
    kernel, db = await _build_cycle_kernel(
        tmp_path,
        _stub_agent(BackendUnreachableError("model backend unreachable")),
        ai=AIConfig(backend="openai_compatible", base_url=base),
    )
    errors = await _collect_system_errors(kernel)

    # (a) usage still metered, under a "failed-" cycle id (degrade intact)
    rows = await db.fetch_all("SELECT cycle_id, input_tokens, api_calls FROM ai_usage")
    assert len(rows) == 1
    assert str(rows[0][0]).startswith("failed-")
    assert rows[0][1] == 10 and rows[0][2] == 1

    # (b) tailored notification: component == model_backend, hint names base_url + LM Studio
    assert len(errors) == 1
    payload = errors[0]
    assert isinstance(payload, dict)
    assert payload["component"] == "model_backend"
    assert base in str(payload["error"])
    assert "LM Studio" in str(payload["error"])
    await db.close()


async def test_generic_agent_error_still_uses_review_cycle_component(tmp_path) -> None:
    # Regression: a non-connect failure must keep publishing under "review_cycle" —
    # the new BackendUnreachableError branch must not steal the generic path.
    from poseidon.core.errors import AgentError

    kernel, db = await _build_cycle_kernel(tmp_path, _stub_agent(AgentError("schema boom")))
    errors = await _collect_system_errors(kernel)

    assert len(errors) == 1
    payload = errors[0]
    assert isinstance(payload, dict)
    assert payload["component"] == "review_cycle"
    assert "schema boom" in str(payload["error"])
    await db.close()
