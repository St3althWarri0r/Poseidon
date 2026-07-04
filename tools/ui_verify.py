#!/usr/bin/env python3
"""End-to-end browser verification of the dashboard (real UI, stub kernel).

Run:  pip install playwright && playwright install chromium && python tools/ui_verify.py
(or set PW_CHROMIUM=/path/to/chrome to use an existing build).

Serves the REAL build_app + static assets over a stub kernel with plausible
data, then drives it with Playwright: renders every view, clicks the new
controls, and screenshots evidence. Exit code 1 on any assertion failure.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from poseidon.api.server import build_app  # noqa: E402
from poseidon.core.config import AppConfig  # noqa: E402
from poseidon.core.enums import MarketSession, OrderSide, TradingMode  # noqa: E402
from poseidon.core.events import EventBus  # noqa: E402
from poseidon.core.models import AccountSnapshot, Fill, Position, Quote  # noqa: E402
from poseidon.portfolio.state import PortfolioState  # noqa: E402

NOW = datetime.now(UTC)
SHOTS = str(__import__("pathlib").Path(__file__).resolve().parent / "ui-verify-shots")
__import__("pathlib").Path(SHOTS).mkdir(exist_ok=True)
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


class FakeDB:
    async def fetch_all(self, sql: str, params=()):  # noqa: ANN001
        if "equity_marks" in sql:
            import math
            points, equity = [], 100_000.0
            for i in range(180):
                at = NOW - timedelta(days=180 - i)
                equity *= 1 + 0.0009 + 0.0007 * math.sin(i / 9)
                points.append((at.isoformat(), equity))
            return list(reversed(points))
        if "decisions" in sql:
            import json
            payload = json.dumps({
                "action": "buy", "cycle_id": "c1", "created_at": NOW.isoformat(),
                "data_sources": ["public_data"], "data_gaps": [], "summary": "s",
                "trades": [{"side": "buy", "quantity": "40", "symbol": "NVDA",
                            "limit_price": "171.40"}],
                "rationale": {"thesis": "t", "timing": "n", "risk": "r", "confidence": 0.7},
            })
            return [(payload,)]
        return []

    async def fetch_one(self, sql: str, params=()):  # noqa: ANN001
        return None


class FakeAudit:
    async def tail(self, limit: int):  # noqa: ANN001
        class Rec:
            def model_dump(self, mode="json"):  # noqa: ANN001
                return {"at": NOW.isoformat(), "actor": "system",
                        "action": "startup", "payload": {"version": "2.3.0"}}
        return [Rec()]

    async def append(self, *a, **k):  # noqa: ANN002, ANN003
        return None


class FakeOrderManager:
    mode = TradingMode.APPROVAL

    async def recent_orders(self, limit=50):  # noqa: ANN001
        return [
            {"id": "o1", "created_at": NOW.isoformat(), "symbol": "NVDA", "side": "buy",
             "quantity": "40", "limit_price": "171.40", "status": "filled",
             "filled_quantity": "40", "avg_fill_price": "171.38",
             "status_reason": None, "slippage_bps": 1.8},
            {"id": "o2", "created_at": NOW.isoformat(), "symbol": "AAPL", "side": "sell",
             "quantity": "60", "limit_price": "212.10", "status": "partially_filled",
             "filled_quantity": "25", "avg_fill_price": "212.15",
             "status_reason": None, "slippage_bps": None},
            {"id": "o3", "created_at": NOW.isoformat(), "symbol": "MSFT", "side": "buy",
             "quantity": "25", "limit_price": "448.30", "status": "submitted",
             "filled_quantity": "0", "avg_fill_price": None,
             "status_reason": None, "slippage_bps": None},
        ]


class FakeApprovals:
    def pending(self):
        from poseidon.core.enums import DecisionAction, OrderType
        from poseidon.core.models import Decision, ExitPlan, Order, TradeRationale

        class Entry:
            pass
        entry = Entry()
        entry.order = Order(symbol="MSFT", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                            quantity=Decimal("25"), limit_price=Decimal("448.30"))
        entry.decision = Decision(
            action=DecisionAction.BUY, trades=[],
            rationale=TradeRationale(
                thesis="t", timing="n", expected_edge="e", risk="r", reward="w",
                confidence=0.7, portfolio_impact="p", exit_plan=ExitPlan(),
                max_expected_loss="$182"),
            data_sources=["public_data"], model="m", cycle_id="c")
        entry.seconds_remaining = 612
        return [entry]


class FakeGuardian:
    async def active_plans(self):
        return [{"symbol": "NVDA", "quantity": "40", "stop_loss": "164.90",
                 "take_profit": "185.00", "time_stop": None,
                 "created_at": NOW.isoformat()}]


class FakeWorkshop:
    def __init__(self):
        self.rows = [
            {"id": "a1", "name": "gap_and_go", "description": "gaps", "source": "async def scan(ctx):\n    return []",
             "symbols": ["NVDA"], "params": {}, "status": "active", "created_by": "user",
             "review_notes": "", "sleeve_pct": 0.2,
             "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()},
            {"id": "a2", "name": "tqqq_day_trader", "description": "draft", "source": "async def scan(ctx):\n    return []",
             "symbols": [], "params": {}, "status": "draft", "created_by": "claude",
             # Marks it as a bundled starter so the Dry Run panel offers to activate it.
             "review_notes": "bundled example — review before activating", "sleeve_pct": 0,
             "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()},
        ]

    async def list_all(self):
        return self.rows

    async def activate(self, algo_id):  # noqa: ANN001
        for r in self.rows:
            if r["id"] == algo_id:
                r["status"] = "active"
                return r
        raise KeyError(algo_id)


class FakeRouter:
    async def quote(self, symbol, allow_delayed=False):  # noqa: ANN001
        return Quote(symbol=symbol, bid=Decimal("448.25"), ask=Decimal("448.35"),
                     last=Decimal("448.30"), as_of=datetime.now(UTC), source="public_data")

    def provider_status(self):
        return [{"name": "public_data", "priority": 10, "available": True,
                 "consecutive_failures": 0, "last_latency_ms": 84.0}]


class FakeBroker:
    name = "paper"
    display_name = "Poseidon Paper"
    is_paper = True
    connected = True
    account_scope = "paper:paper"


class FakeClock:
    def session(self):
        return MarketSession.REGULAR


class FakeVault:
    def names(self):
        return ["anthropic_api_key", "finnhub_api_key"]


class FakeChat:
    busy = False

    def __init__(self):
        self.log = []

    async def history(self, limit=200):  # noqa: ANN001
        return list(self.log)

    async def clear(self):
        self.log = []


class FakeSync:
    async def sync_once(self):
        return None


class FakeKernel:
    def __init__(self):
        self.bus = EventBus()
        self.config = AppConfig()
        self.db = FakeDB()
        self.audit = FakeAudit()
        self.order_manager = FakeOrderManager()
        self.approvals = FakeApprovals()
        self.guardian = FakeGuardian()
        self.router = FakeRouter()
        self.workshop = FakeWorkshop()
        self.broker = FakeBroker()
        self.clock = FakeClock()
        self.vault = FakeVault()
        self.chat = FakeChat()
        self.sync = FakeSync()
        self.mode = TradingMode.APPROVAL
        self.portfolio = PortfolioState()
        self.portfolio.account = AccountSnapshot(
            broker="paper", account_id="SIM-1", equity=Decimal("118472.55"),
            cash=Decimal("24880.10"), buying_power=Decimal("24880.10"),
            day_pnl=Decimal("642.18"), as_of=NOW)
        self.portfolio.synced_at = NOW
        self.portfolio.day_start_equity = Decimal("117830.37")
        self.portfolio.peak_equity = Decimal("119510.00")

        def mk(s, q, avg, mv, pnl):  # noqa: ANN001
            return Position(symbol=s, quantity=Decimal(q), avg_entry_price=Decimal(avg),
                            market_value=Decimal(mv), unrealized_pnl=Decimal(pnl),
                            broker="paper", as_of=NOW)
        self.portfolio.positions = [
            mk("NVDA", "40", "168.20", "6855.20", "127.20"),
            mk("SPY", "50", "601.10", "30455.00", "400.00"),
        ]
        self.portfolio.recent_fills = [
            Fill(order_id="o1", symbol="NVDA", side=OrderSide.BUY, quantity=Decimal("40"),
                 price=Decimal("171.38"), filled_at=NOW),
        ]

    async def chat_message(self, message: str):
        self.chat.log.append({"role": "user", "content": message, "at": NOW.isoformat()})
        reply = f"STUB-REPLY to: {message}"
        self.chat.log.append({"role": "assistant", "content": reply, "at": NOW.isoformat()})
        return {"reply": reply, "tool_calls": ["get_quote"], "usage": {"api_calls": 1}}

    async def broker_connection_test(self, name, *, paper, credentials, options=None):  # noqa: ANN001
        return {"display_name": "Poseidon Paper", "account_id": "SIM-1", "equity": "118472.55",
                "cash": "24880.10", "buying_power": "24880.10", "paper": True}

    async def switch_broker(self, name, *, paper, credentials, options=None):  # noqa: ANN001
        return {"name": name, "display_name": name, "paper": paper,
                "account_id": "SIM-1", "equity": "118472.55", "provider_note": ""}

    async def status_report(self):
        active = sum(1 for r in self.workshop.rows if r["status"] == "active")
        return {
            "version": "2.3.0", "mode": self.mode.value, "cycle_running": False,
            "market_session": "regular",
            "broker": {"name": "paper", "paper": True, "connected": True},
            "providers": self.router.provider_status(),
            "risk": {"circuit_open": False, "circuit_reason": None, "orders_today": 3,
                     "max_orders_per_day": 40, "day_loss_pct": 0.0, "week_loss_pct": 0.01,
                     "drawdown_pct": 0.008,
                     "limits": {"max_daily_loss_pct": 0.03, "max_weekly_loss_pct": 0.07,
                                "max_drawdown_pct": 0.15}},
            "health": {"overall": "healthy", "components": {}},
            "scheduler": {}, "update_available": False,
            "ai_usage": {"cycles": 4, "api_calls": 21, "input_tokens": 100, "output_tokens": 50,
                         "cache_read_tokens": 0, "cache_write_tokens": 0,
                         "month_cost_usd": 1.2, "monthly_budget_usd": None},
            "guardian": {"enabled": True, "active_plans": await self.guardian.active_plans()},
            "_active_algos": active,
        }

    async def refresh_risk_metrics(self):
        return None

    async def performance_report(self):
        return {"total_return": 0.18, "cagr": 0.4, "annualized_volatility": 0.15,
                "sharpe": 1.8, "sortino": 2.6, "max_drawdown": 0.06, "calmar": 6.6,
                "trades": 10, "win_rate": 0.6, "profit_factor": 1.9, "avg_win": 400.0,
                "avg_loss": -180.0, "expectancy": 140.0, "avg_holding_days": 6.0,
                "realized_pnl": 8000.0, "monthly_returns": {}, "by_strategy": {}}

    async def execution_report(self, limit=500):  # noqa: ANN001
        return {"orders_filled": 10, "orders_reaching_broker": 11, "fill_rate": 0.9,
                "orders_measured": 10, "avg_slippage_bps": 1.4, "median_slippage_bps": 0.9,
                "worst_slippage_bps": 9.6, "worst_fill": None,
                "avg_slippage_bps_by_side": {"buy": 2.1, "sell": 0.7},
                "avg_slippage_bps_by_symbol": {}, "avg_seconds_to_fill": 11.3}

    async def set_mode(self, mode):  # noqa: ANN001
        self.mode = mode
        self.order_manager.mode = mode


async def drive() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        import os
        exe = os.environ.get("PW_CHROMIUM")  # override when a preinstalled build exists
        browser = await pw.chromium.launch(executable_path=exe) if exe \
            else await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        base = "http://127.0.0.1:8399"
        await page.goto(base, wait_until="networkidle")

        # -- Overview: Automation tile ------------------------------------
        await page.wait_for_timeout(600)
        auto = await page.text_content("#t-auto-sub")
        check("overview automation tile", auto is not None and "algorithm" in auto, repr(auto))
        await page.screenshot(path=f"{SHOTS}/v-overview.png")

        # -- Portfolio: Close button, notional, Fill column, fills tape ---
        await page.click('a[data-view="portfolio"]')
        await page.wait_for_timeout(700)
        close_btns = await page.locator("[data-close-pos]").count()
        check("positions Close buttons", close_btns == 2, f"count={close_btns}")
        await page.click('[data-close-pos="NVDA"]')
        await page.wait_for_timeout(600)
        sym = await page.input_value("#tk-symbol")
        side = await page.input_value("#tk-side")
        qty = await page.input_value("#tk-qty")
        check("close prefills ticket", sym == "NVDA" and side == "sell" and qty == "40",
              f"{sym}/{side}/{qty}")
        notional = await page.text_content("#tk-notional")
        check("notional readout", notional is not None and "notional" in notional.lower(),
              repr(notional))
        header = await page.text_content("#orders-table thead")
        check("orders Fill column", header is not None and "Fill" in header)
        fill_cell = await page.text_content("#orders-table tbody")
        check("partial fill visible", fill_cell is not None and "25/60" in fill_cell)
        hint = await page.text_content("#orders-hint")
        # o2 (partially_filled) + o3 (submitted) are both working
        check("working-order count", hint is not None and "2 working" in hint, repr(hint))
        cancel_all_visible = await page.locator("#orders-cancel-all").is_visible()
        check("cancel-all visible with 2 working", cancel_all_visible)
        fills = await page.text_content("#fills")
        check("fills tape", fills is not None and "NVDA" in fills)
        # probe: limit order without a price -> client-side toast, no request
        await page.fill("#tk-limit", "")
        await page.click("#tk-submit")
        await page.wait_for_timeout(300)
        toasts = await page.text_content("#toasts")
        check("probe: empty limit blocked", toasts is not None and "limit price" in toasts)
        await page.screenshot(path=f"{SHOTS}/v-portfolio.png")

        # -- AI Desk: chat + ticking countdown -----------------------------
        await page.click('a[data-view="ai"]')
        await page.wait_for_timeout(700)
        check("chat form present", await page.locator("#chat-form").count() == 1)
        await page.fill("#chat-input", "how is NVDA?")
        await page.click("#chat-send")
        await page.wait_for_timeout(800)
        log = await page.text_content("#chat-log")
        check("chat round-trip", log is not None and "STUB-REPLY to: how is NVDA?" in log)
        check("chat tool meta", log is not None and "get_quote" in log)
        t1 = await page.text_content("#approvals .expiry[data-deadline]")
        await page.wait_for_timeout(2100)
        t2 = await page.text_content("#approvals .expiry[data-deadline]")
        check("approval countdown ticks", t1 is not None and t2 is not None and t1 != t2,
              f"{t1!r} -> {t2!r}")
        await page.screenshot(path=f"{SHOTS}/v-ai.png")

        # -- Account: tiles, paper starting cash, live warning, cost note --
        await page.click('a[data-view="account"]')
        await page.wait_for_timeout(700)
        tiles = await page.locator(".broker-tile").count()
        check("broker tiles render", tiles >= 7, f"count={tiles}")
        stub_box = await page.locator("#broker-stubs").count()
        check("stub box removed", stub_box == 0)
        await page.click('[data-broker="paper"]')
        await page.wait_for_timeout(300)
        cash_field = await page.locator('#bf-options [data-opt="starting_cash"]').count()
        check("paper starting-cash field", cash_field == 1)
        await page.click('[data-broker="public"]')
        await page.wait_for_timeout(300)
        warning_hidden = await page.get_attribute("#bf-live-warning", "hidden")
        check("live warning for public", warning_hidden is None)
        paper_row_visible = await page.locator("#bf-paper-row").is_visible()
        check("paper toggle hidden for live-only", not paper_row_visible)
        await page.click('[data-broker="ibkr"]')
        await page.wait_for_timeout(300)
        cost = await page.text_content("#bf-cost")
        check("ibkr cost note", cost is not None and "fee" in cost.lower(), repr(cost))
        # probe: connect with empty required fields on alpaca
        await page.click('[data-broker="alpaca"]')
        await page.wait_for_timeout(300)
        await page.click("#bf-connect")
        await page.wait_for_timeout(300)
        toasts = await page.text_content("#toasts")
        check("probe: empty credentials blocked", toasts is not None and "credential" in toasts)
        # -- Schwab in-app OAuth: "Log in with Schwab" reaches Schwab's login --
        await page.click('[data-broker="schwab"]')
        await page.wait_for_timeout(300)
        oauth_visible = await page.locator("#bf-oauth").is_visible()
        check("schwab oauth block shown", oauth_visible)
        login_label = await page.text_content("#bf-oauth-login")
        check("schwab login button", login_label is not None and "Log in with" in login_label,
              repr(login_label))
        # Capture the window.open target instead of navigating to the real
        # Schwab page, so the check is deterministic and offline.
        await page.evaluate("window.__openedUrl=null; window.open=(u)=>{window.__openedUrl=u;return null;}")
        await page.fill('#bf-fields [data-cred="app_key"]', "TESTAPPKEY")
        await page.click("#bf-oauth-login")
        await page.wait_for_timeout(400)
        opened = await page.evaluate("window.__openedUrl")
        check("schwab login opens Schwab OAuth authorize screen",
              opened is not None
              and opened.startswith("https://api.schwabapi.com/v1/oauth/authorize")
              and "client_id=TESTAPPKEY" in opened,
              repr(opened))
        toasts = await page.text_content("#toasts")
        check("schwab login toast", toasts is not None and "login page" in toasts.lower(),
              repr(toasts))
        await page.screenshot(path=f"{SHOTS}/v-schwab-oauth.png")
        # OAuth block must NOT show for a non-OAuth broker
        await page.click('[data-broker="public"]')
        await page.wait_for_timeout(300)
        oauth_hidden = await page.get_attribute("#bf-oauth", "hidden")
        check("oauth block hidden for non-oauth broker", oauth_hidden is not None)

        await page.click("#acct-sync")
        await page.wait_for_timeout(500)
        toasts = await page.text_content("#toasts")
        check("sync now", toasts is not None and "synced" in toasts.lower(), repr(toasts))
        await page.screenshot(path=f"{SHOTS}/v-account.png")

        # -- Algorithms: auto-invest flow ----------------------------------
        await page.click('a[data-view="algorithms"]')
        await page.wait_for_timeout(700)
        await page.click('[data-algo="a2"]')  # the draft
        await page.wait_for_timeout(300)
        btn_visible = await page.locator("#al-autoinvest").is_visible()
        state = await page.text_content("#al-autostate")
        check("auto-invest button on draft", btn_visible)
        check("auto-invest state line", state is not None and "autonomous" in state.lower(),
              repr(state))
        await page.click("#al-autoinvest")  # dialog auto-accepted
        await page.wait_for_timeout(900)
        toasts = await page.text_content("#toasts")
        check("auto-invest starts", toasts is not None and "Auto-investing" in toasts, repr(toasts))
        chip = await page.text_content("#algo-list")
        check("auto-investing chip", chip is not None and "auto-investing" in chip)
        await page.screenshot(path=f"{SHOTS}/v-algorithms.png")

        # -- Remaining views render without JS errors ----------------------
        for view in ("risk", "performance", "system"):
            await page.click(f'a[data-view="{view}"]')
            await page.wait_for_timeout(500)
            await page.screenshot(path=f"{SHOTS}/v-{view}.png")

        check("no JS page errors", not js_errors, "; ".join(js_errors[:3]))
        await browser.close()


async def main() -> None:
    import uvicorn

    app = build_app(FakeKernel())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8399,
                                           log_level="error"))
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)
    try:
        await drive()
    finally:
        server.should_exit = True
        await task
    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)


asyncio.run(main())
