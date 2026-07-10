"""Dashboard API server.

Binds to localhost only by default. On a non-loopback bind a bearer token
is mandatory — config validation refuses to start without one (see
core/config.py) — and is checked constant-time on every API request and
websocket handshake; only /static assets are exempt. The token travels in
clear over plain HTTP, so for remote access still prefer an SSH tunnel or
an authenticated TLS reverse proxy (docs/security.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..ai.chat import ChatBusyError
from ..brokers.registry import broker_catalog
from ..core.enums import MarketSession, TradingMode
from ..core.errors import (
    AgentError,
    BrokerAuthError,
    BrokerError,
    ConfigError,
    DataError,
    VaultError,
)
from ..core.events import EventBus
from ..terminal.routes import router as terminal_router

if TYPE_CHECKING:
    from ..app import ApplicationKernel

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_dryrun_state(*, broker_is_paper: bool, active_broker: str, mode_value: str,
                       algorithms_raw: list[dict[str, Any]], session: MarketSession) -> dict[str, Any]:
    """Aggregate the Dry Run panel's state from plain inputs (pure, testable)."""
    from ..strategy.workshop import BUNDLED_REVIEW_NOTE
    algorithms = [
        {"id": a["id"], "name": a["name"], "status": a["status"],
         "bundled": a.get("review_notes") == BUNDLED_REVIEW_NOTE}
        for a in algorithms_raw
    ]
    is_open = session is MarketSession.REGULAR
    return {
        "broker_is_paper": broker_is_paper,
        "active_broker": active_broker,
        "mode": mode_value,
        "algorithms": algorithms,
        "active_algo_count": sum(1 for a in algorithms if a["status"] == "active"),
        "bundled_draft_count": sum(1 for a in algorithms
                                   if a["bundled"] and a["status"] == "draft"),
        "market": {"session": session.value, "is_open": is_open,
                   "opens_hint": None if is_open else "9:30 ET"},
    }


class WebsocketHub:
    """Fan out every bus event to connected dashboard clients."""

    def __init__(self, bus: EventBus) -> None:
        self._clients: set[WebSocket] = set()
        bus.subscribe("*", self._on_event)

    async def _on_event(self, topic: str, payload: Any) -> None:
        if not self._clients:
            return
        message = json.dumps({"topic": topic, "payload": payload}, default=str)
        # Send to every client concurrently, each bounded by a deadline, so a
        # single stalled peer (e.g. a suspended laptop) cannot delay delivery
        # to the others or pile up tasks behind its full send buffer.
        clients = list(self._clients)
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(message), timeout=5.0) for ws in clients),
            return_exceptions=True,
        )
        for ws, result in zip(clients, results, strict=True):
            if isinstance(result, Exception):
                # Send failed or the peer stalled past the deadline: drop it.
                # handle()'s receive loop will error and finish cleanup.
                self._clients.discard(ws)

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        try:
            while True:
                await ws.receive_text()  # keepalive pings from the client
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(ws)


def build_app(kernel: ApplicationKernel) -> FastAPI:
    app = FastAPI(title="Poseidon", docs_url=None, redoc_url=None, openapi_url=None)
    hub = WebsocketHub(kernel.bus)

    # Strong references to fire-and-forget background tasks: the event loop
    # keeps only weak refs, so an unreferenced task can be GC'd mid-run.
    background_tasks: set[asyncio.Task[None]] = set()

    def _bg_done(task: asyncio.Task[None]) -> None:
        background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", task=task.get_name(),
                      error=str(task.exception()))

    # DNS-rebinding protection: a malicious website resolving to 127.0.0.1
    # sends its own domain in the Host header; reject anything that is not a
    # genuine loopback/configured host. On a non-loopback bind the operator
    # may reach it by any address, so the (mandatory) bearer token is the
    # guard instead.
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    configured_host = kernel.config.dashboard.host
    if configured_host in ("127.0.0.1", "localhost", "::1"):
        app.add_middleware(TrustedHostMiddleware,
                           allowed_hosts=["127.0.0.1", "localhost", "::1"])

    # The origins a legitimate dashboard page is served from. Used to reject
    # cross-site state-changing requests and cross-origin websocket handshakes
    # — TrustedHost only stops DNS rebinding, not a direct-IP CSRF from a page
    # the operator has open in the same browser.
    _port = kernel.config.dashboard.port
    allowed_origins: set[str] = set()
    for _host in ("127.0.0.1", "localhost", "::1", configured_host):
        allowed_origins.add(f"http://{_host}:{_port}")
        allowed_origins.add(f"https://{_host}:{_port}")

    def _origin_allowed(origin: str, host_header: str) -> bool:
        """Besides the static allowlist (loopbacks + configured host), accept
        the origin matching the request's own Host header: on a non-loopback
        bind the operator reaches the dashboard by a machine address or via a
        reverse proxy, and the browser's Origin carries that address, not the
        bind address. Origin==Host does not weaken the guard — DNS rebinding
        is stopped by TrustedHostMiddleware on loopback binds and by the
        mandatory bearer token on non-loopback binds."""
        if origin in allowed_origins:
            return True
        return bool(host_header) and origin in (
            f"http://{host_header}", f"https://{host_header}")

    # CSRF guard: runs for every request regardless of whether a bearer token
    # is configured. Blocks unsafe methods issued cross-site (the parameterless
    # POSTs — /api/resume, /api/halt, /api/cycle, /api/sync — are otherwise
    # reachable as CORS "simple" requests with no preflight). Modern browsers
    # always send Sec-Fetch-Site; Origin is the fallback. A request with
    # neither header (a non-browser client like curl) is not a CSRF vector and
    # is allowed — the loopback bind already constrains reachability.
    from starlette.requests import Request as _Request
    from starlette.responses import JSONResponse as _StarletteJSON

    _unsafe_methods = {"POST", "PUT", "DELETE", "PATCH"}

    @app.middleware("http")
    async def _csrf_guard(request: _Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in _unsafe_methods and not request.url.path.startswith("/static"):
            sec_fetch_site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            if sec_fetch_site is not None:
                if sec_fetch_site not in ("same-origin", "none"):
                    return _StarletteJSON({"detail": "cross-site request blocked"}, status_code=403)
            elif origin is not None and not _origin_allowed(
                    origin, request.headers.get("host", "")):
                return _StarletteJSON({"detail": "cross-origin request blocked"}, status_code=403)
        return await call_next(request)

    # Optional bearer-token auth (required by config validation whenever the
    # host is non-loopback). Static assets are exempt — they contain nothing
    # sensitive and <link>/<script> tags cannot carry headers.
    auth_token: str | None = None
    if kernel.config.dashboard.auth_token_credential:
        auth_token = kernel.vault.get(kernel.config.dashboard.auth_token_credential)
    else:
        # Fallback: a token supplied directly via env/secret file (container
        # deployments that cannot pre-seed the vault). See config.py.
        from ..core.config import dashboard_token_from_env
        auth_token = dashboard_token_from_env()

    def _token_ok(supplied: str | None) -> bool:
        import hmac

        return auth_token is not None and supplied is not None and hmac.compare_digest(
            supplied, auth_token
        )

    if auth_token:
        from starlette.requests import Request
        from starlette.responses import JSONResponse as StarletteJSON

        @app.middleware("http")
        async def _require_token(request: Request, call_next):  # type: ignore[no-untyped-def]
            # /static and the embedded market-study terminal are token-exempt:
            # static assets carry no secrets and cannot send headers from
            # <script>/<link>; /terminal + /api/terminal are read-only public
            # market data (keyless Yahoo) — no account, positions, or broker
            # state is reachable through them (spec addendum 2026-07-09).
            if request.url.path.startswith(("/static", "/terminal", "/api/terminal")):
                return await call_next(request)
            supplied = request.query_params.get("token")
            header = request.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                supplied = header.removeprefix("Bearer ")
            if not _token_ok(supplied):
                return StarletteJSON({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

    @app.get("/")
    async def index() -> Response:
        # Version-stamp the asset URLs (?v=x.y.z) so a browser can never pair
        # a new backend with cached old JS/CSS — the "I updated but see the
        # old dashboard" class of confusion.
        from .. import __version__

        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__V__", __version__),
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(await kernel.status_report())

    @app.get("/api/portfolio")
    async def portfolio() -> JSONResponse:
        state = kernel.portfolio.snapshot_dict()
        state["tax_lots"] = [t.model_dump(mode="json") for t in kernel.portfolio.tax_lots]
        state["recent_fills"] = [f.model_dump(mode="json") for f in kernel.portfolio.recent_fills[-25:]]
        return JSONResponse(state)

    @app.get("/api/equity")
    async def equity(limit: int = 2000) -> JSONResponse:
        # Scoped to the active broker: a paper curve and a real-account curve
        # must never be spliced into one line.
        rows = await kernel.db.fetch_all(
            "SELECT at, equity FROM equity_marks WHERE broker = ? ORDER BY at DESC LIMIT ?",
            (kernel.broker.account_scope, limit),
        )
        points = [{"at": r[0], "equity": float(r[1])} for r in reversed(rows)]
        return JSONResponse({"points": points})

    @app.get("/api/orders")
    async def orders(limit: int = 50) -> JSONResponse:
        return JSONResponse({"orders": await kernel.order_manager.recent_orders(limit)})

    @app.get("/api/decisions")
    async def decisions(limit: int = 25) -> JSONResponse:
        rows = await kernel.db.fetch_all(
            "SELECT payload FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return JSONResponse({"decisions": [json.loads(r[0]) for r in rows]})

    @app.get("/api/approvals")
    async def approvals() -> JSONResponse:
        pending = kernel.approvals.pending()
        return JSONResponse({
            "approvals": [
                {
                    "order": e.order.model_dump(mode="json"),
                    "rationale": e.decision.rationale.model_dump(mode="json")
                    if e.decision.rationale else None,
                    "seconds_remaining": int(e.seconds_remaining),
                }
                for e in pending
            ]
        })

    @app.post("/api/approvals/{order_id}")
    async def resolve_approval(order_id: str, body: dict[str, Any]) -> JSONResponse:
        # Require an explicit JSON boolean: bool("false") is True, so coercing
        # a stringified boolean here would approve a real-money trade a
        # scripted client meant to reject.
        approve = body.get("approve")
        if not isinstance(approve, bool):
            raise HTTPException(status_code=422,
                                detail="approve must be a JSON boolean (true/false)")
        try:
            kernel.approvals.resolve(order_id, approved=approve)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await kernel.audit.append("human", "approval.resolved",
                                  {"order_id": order_id, "approved": approve})
        return JSONResponse({"ok": True})

    @app.post("/api/orders/{order_id}/cancel")
    async def cancel_order(order_id: str) -> JSONResponse:
        try:
            order = await kernel.order_manager.cancel(order_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConfigError as exc:  # broker switch in progress / wrong broker
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "status": order.status.value})

    @app.post("/api/mode")
    async def set_mode(body: dict[str, Any]) -> JSONResponse:
        try:
            mode = TradingMode(str(body.get("mode", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="mode must be research|approval|autonomous") from exc
        await kernel.set_mode(mode)
        return JSONResponse({"ok": True, "mode": mode.value})

    @app.post("/api/halt")
    async def halt(body: dict[str, Any] | None = None) -> JSONResponse:
        reason = (body or {}).get("reason", "manual halt from dashboard")
        kernel.risk.circuit.force_open(str(reason))
        await kernel.audit.append("human", "trading.halted", {"reason": reason})
        return JSONResponse({"ok": True})

    @app.post("/api/resume")
    async def resume() -> JSONResponse:
        kernel.risk.circuit.force_close()
        await kernel.audit.append("human", "trading.resumed", {})
        return JSONResponse({"ok": True})

    @app.post("/api/cycle")
    async def trigger_cycle() -> JSONResponse:
        task = asyncio.create_task(kernel.run_review_cycle(), name="api-review-cycle")
        background_tasks.add(task)
        task.add_done_callback(_bg_done)
        return JSONResponse({"ok": True, "detail": "review cycle started"})

    @app.get("/api/performance")
    async def performance() -> JSONResponse:
        return JSONResponse(await kernel.performance_report())

    @app.get("/api/exit-plans")
    async def exit_plans() -> JSONResponse:
        return JSONResponse({"plans": await kernel.guardian.active_plans()})

    @app.get("/api/quote/{symbol}")
    async def quote(symbol: str) -> JSONResponse:
        """Quote for the trade ticket. During regular hours only a FRESH quote
        is shown (a human about to trade sees the same freshness bar as the
        AI). Outside regular hours a fresh print cannot exist, so the last
        real trade is returned clearly flagged ``reference: true`` — display
        only; order submission still requires a fresh quote in the risk
        engine, always."""
        from ..core.enums import MarketSession
        from ..core.errors import StaleDataError

        session = kernel.clock.session().value
        try:
            q = await kernel.router.quote(symbol.upper(), allow_delayed=False)
            return JSONResponse({**q.model_dump(mode="json"),
                                 "session": session, "reference": False})
        except StaleDataError as exc:
            if kernel.clock.session() is MarketSession.REGULAR:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # Market not in regular session: serve the last print, labeled.
        try:
            q = await kernel.router.reference_quote(symbol.upper())
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({**q.model_dump(mode="json"),
                             "session": session, "reference": True})

    @app.post("/api/trade")
    async def trade(body: dict[str, Any]) -> JSONResponse:
        """Manual order entry. Same pipeline as AI orders: full risk engine,
        duplicate guard, broker preflight, lifecycle polling, audit."""
        from decimal import Decimal, InvalidOperation

        from ..core.enums import AssetClass, OrderSide, OrderType, TimeInForce
        from ..core.models import Order

        extended_hours = body.get("extended_hours", False)
        if not isinstance(extended_hours, bool):
            raise HTTPException(status_code=422, detail="extended_hours must be a JSON boolean")
        try:
            order = Order(
                symbol=str(body["symbol"]).upper().strip(),
                asset_class=AssetClass(str(body.get("asset_class", "equity"))),
                side=OrderSide(str(body["side"])),
                order_type=OrderType(str(body.get("order_type", "limit"))),
                quantity=Decimal(str(body["quantity"])),
                limit_price=Decimal(str(body["limit_price"])) if body.get("limit_price") else None,
                stop_price=Decimal(str(body["stop_price"])) if body.get("stop_price") else None,
                time_in_force=TimeInForce(str(body.get("time_in_force", "day"))),
                extended_hours=extended_hours,
                strategy="manual",
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise HTTPException(status_code=422, detail=f"invalid order: {exc}") from exc
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.limit_price is None:
            raise HTTPException(status_code=422, detail="limit orders need a limit_price")
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and order.stop_price is None:
            raise HTTPException(status_code=422, detail="stop orders need a stop_price")
        result = await kernel.order_manager.submit_manual(order)
        return JSONResponse({
            "order": result.model_dump(mode="json"),
            "accepted": result.status.value not in ("rejected_risk", "rejected_human", "rejected_broker", "error"),
        })

    @app.get("/api/risk-metrics")
    async def risk_metrics(refresh: bool = False) -> JSONResponse:
        cached = kernel.portfolio.risk_metrics
        if refresh or cached is None:
            try:
                cached = await kernel.refresh_risk_metrics()
            except Exception as exc:  # data unavailable: report, don't fabricate
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(cached)

    @app.get("/api/execution")
    async def execution(limit: int = 500) -> JSONResponse:
        return JSONResponse(await kernel.execution_report(limit=limit))

    # ---- algorithm workshop ----

    @app.get("/api/algorithms")
    async def algorithms() -> JSONResponse:
        return JSONResponse({"algorithms": await kernel.workshop.list_all()})

    @app.post("/api/algorithms")
    async def create_algorithm(body: dict[str, Any]) -> JSONResponse:
        from ..core.errors import ConfigError as CfgErr
        try:
            record = await kernel.workshop.create(
                name=str(body.get("name", "")), source=str(body.get("source", "")),
                description=str(body.get("description", "")),
                symbols=[str(s) for s in body.get("symbols", [])],
                params=dict(body.get("params", {})),
                created_by="user", review_notes=str(body.get("review_notes", "")),
                sleeve_pct=float(body.get("sleeve_pct", 0) or 0),
            )
        except (CfgErr, ValueError, TypeError) as exc:
            # ValueError/TypeError: malformed numeric/iterable fields (e.g.
            # sleeve_pct="5%") — a 422, not an uncaught 500. Matches update_algorithm.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"algorithm": record})

    @app.put("/api/algorithms/{algo_id}")
    async def update_algorithm(algo_id: str, body: dict[str, Any]) -> JSONResponse:
        from ..core.errors import ConfigError as CfgErr
        try:
            record = await kernel.workshop.update(
                algo_id,
                source=body.get("source"), description=body.get("description"),
                symbols=body.get("symbols"), params=body.get("params"),
                review_notes=body.get("review_notes"),
                sleeve_pct=(float(body["sleeve_pct"]) if body.get("sleeve_pct")
                            is not None else None),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CfgErr, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"algorithm": record})

    @app.post("/api/algorithms/{algo_id}/activate")
    async def activate_algorithm(algo_id: str) -> JSONResponse:
        try:
            record = await kernel.workshop.activate(algo_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"algorithm": record})

    @app.post("/api/algorithms/{algo_id}/deactivate")
    async def deactivate_algorithm(algo_id: str, body: dict[str, Any] | None = None) -> JSONResponse:
        archive = (body or {}).get("archive", False)
        if not isinstance(archive, bool):
            raise HTTPException(status_code=422, detail="archive must be a JSON boolean")
        try:
            record = await kernel.workshop.deactivate(algo_id, archive=archive)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse({"algorithm": record})

    @app.delete("/api/algorithms/{algo_id}")
    async def delete_algorithm(algo_id: str) -> JSONResponse:
        try:
            await kernel.workshop.delete(algo_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/api/algorithms/{algo_id}/test")
    async def test_algorithm(algo_id: str) -> JSONResponse:
        try:
            result = await kernel.workshop.test_run(algo_id, kernel.router, kernel.portfolio)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/algorithms/{algo_id}/backtest")
    async def backtest_algorithm(algo_id: str, body: dict[str, Any] | None = None) -> JSONResponse:
        payload = body or {}
        try:
            result = await kernel.workshop.backtest(
                algo_id, kernel.router, kernel.portfolio,
                years=int(payload.get("years", 5)),
                period=payload.get("period"),
                start=payload.get("start"), end=payload.get("end"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/algorithms/review")
    async def review_algorithm(body: dict[str, Any]) -> JSONResponse:
        source = str(body.get("source", "")).strip()
        if not source:
            raise HTTPException(status_code=422, detail="paste the algorithm source to review")
        try:
            review = await kernel.review_algorithm(
                source=source, instructions=str(body.get("instructions", ""))
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"review failed: {exc}") from exc
        return JSONResponse({"review": review})

    # ------------------------------------------------- account / broker connect

    @app.get("/api/brokers")
    async def brokers() -> JSONResponse:
        catalog = broker_catalog()
        saved_names: set[str] = set()
        with contextlib.suppress(Exception):
            saved_names = set(kernel.vault.names())
        for entry in catalog:
            cred = str(entry.get("credential", ""))
            entry["credential_saved"] = bool(cred) and cred in saved_names
            entry["is_current"] = entry["name"] == kernel.broker.name
        acct = kernel.portfolio.account
        return JSONResponse({
            "current": {
                "name": kernel.broker.name,
                "display_name": kernel.broker.display_name or kernel.broker.name,
                "paper": kernel.broker.is_paper,
                "connected": kernel.broker.connected,
                "account_id": acct.account_id if acct else None,
                "equity": str(acct.equity) if acct else None,
                "cash": str(acct.cash) if acct else None,
                "buying_power": str(acct.buying_power) if acct else None,
                "synced_at": (kernel.portfolio.synced_at.isoformat()
                              if kernel.portfolio.synced_at else None),
            },
            "brokers": catalog,
            "mode": kernel.order_manager.mode.value,
        })

    def _broker_request(
        body: dict[str, Any],
    ) -> tuple[str, bool, dict[str, str] | None, dict[str, object]]:
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=422, detail="broker name required")
        # An empty string would coerce to paper=False, silently selecting the
        # LIVE account — require an explicit boolean when the key is present.
        paper = body.get("paper", True)
        if not isinstance(paper, bool):
            raise HTTPException(status_code=422, detail="paper must be a JSON boolean")
        credentials: dict[str, str] | None = None
        raw = body.get("credentials")
        if isinstance(raw, dict):
            # Drop blank optional fields so plugins see only real values.
            credentials = {str(k): str(v).strip() for k, v in raw.items() if str(v).strip()}
        options: dict[str, object] = {}
        raw_options = body.get("options")
        if isinstance(raw_options, dict):
            # Whitelist: only the option keys the catalog advertises for this
            # broker (plus the one-shot reset flag) may cross the API — never
            # internal plugin knobs like state_file or read_only.
            entry = next((e for e in broker_catalog() if e["name"] == name), None)
            allowed: set[str] = {"reset"}
            if entry is not None:
                fields = entry.get("option_fields")
                if isinstance(fields, list):
                    allowed.update(str(f["key"]) for f in fields
                                   if isinstance(f, dict) and "key" in f)
            options = {str(k): v for k, v in raw_options.items()
                       if str(k) in allowed and str(v).strip() != ""}
        return name, paper, credentials, options

    @app.post("/api/brokers/test")
    async def broker_test(body: dict[str, Any]) -> JSONResponse:
        """Prove auth + account access without changing anything. Failures are
        a normal outcome here, so they come back 200 with ok=false."""
        name, paper, credentials, options = _broker_request(body)
        try:
            account = await kernel.broker_connection_test(
                name, paper=paper, credentials=credentials, options=options)
        except (BrokerError, ConfigError, VaultError, DataError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True, "account": account})

    @app.post("/api/brokers/connect")
    async def broker_connect(body: dict[str, Any]) -> JSONResponse:
        """Store credentials in the vault, persist the choice, and hot-swap
        the active broker. The trading mode is untouched — connecting a live
        account in research mode still cannot place an order."""
        name, paper, credentials, options = _broker_request(body)
        try:
            result = await kernel.switch_broker(
                name, paper=paper, credentials=credentials, options=options)
        except (BrokerError, ConfigError, VaultError, DataError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "broker": result})

    @app.post("/api/brokers/schwab/authorize-url")
    async def schwab_authorize_url(body: dict[str, Any]) -> JSONResponse:
        """Build the Schwab OAuth login URL for the entered app key. POST (not
        GET) so the app key never lands in access logs / browser history."""
        from ..brokers.plugins.schwab import DEFAULT_REDIRECT_URI, SchwabBroker
        app_key = str(body.get("app_key", "")).strip()
        if not app_key:
            raise HTTPException(status_code=422, detail="app_key required")
        redirect_uri = str(body.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip()
        return JSONResponse({
            "url": SchwabBroker.authorize_url(app_key, redirect_uri),
            "redirect_uri": redirect_uri,
        })

    @app.post("/api/brokers/schwab/exchange")
    async def schwab_exchange(body: dict[str, Any]) -> JSONResponse:
        """Finish the Schwab login: swap the pasted redirect URL's code for a
        refresh token and resolve the account hash, so the Connect form's
        credential fields can be filled in without leaving the dashboard."""
        from ..brokers.plugins.schwab import DEFAULT_REDIRECT_URI, SchwabBroker
        app_key = str(body.get("app_key", "")).strip()
        app_secret = str(body.get("app_secret", "")).strip()
        pasted = str(body.get("redirect_response", "")).strip()
        redirect_uri = str(body.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip()
        if not (app_key and app_secret and pasted):
            raise HTTPException(
                status_code=422,
                detail="app_key, app_secret, and the pasted redirect URL are all required")
        try:
            code = SchwabBroker.extract_code(pasted)
            tokens = await SchwabBroker.exchange_code(
                app_key=app_key, app_secret=app_secret, code=code, redirect_uri=redirect_uri)
            account_hash = ""
            access = str(tokens.get("access_token", ""))
            if access:
                with contextlib.suppress(BrokerError):
                    account_hash = await SchwabBroker.fetch_account_hash(access)
        except (BrokerError, BrokerAuthError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({
            "ok": True,
            "refresh_token": tokens["refresh_token"],
            "account_hash": account_hash,
        })

    @app.get("/api/dryrun")
    async def dryrun_state() -> JSONResponse:
        """Everything the Dry Run panel needs, in one read."""
        return JSONResponse(build_dryrun_state(
            broker_is_paper=kernel.broker.is_paper,
            active_broker=kernel.broker.name,
            mode_value=kernel.order_manager.mode.value,
            algorithms_raw=await kernel.workshop.list_all(),
            session=kernel.clock.session(),
        ))

    @app.post("/api/sync")
    async def sync_now() -> JSONResponse:
        """Manual portfolio sync — pull the account from the broker right now."""
        try:
            await kernel.sync.sync_once()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"sync failed: {exc}") from exc
        return JSONResponse({"ok": True,
                             "synced_at": kernel.portfolio.synced_at.isoformat()
                             if kernel.portfolio.synced_at else None})

    # ----------------------------------------------------------------- AI chat

    @app.get("/api/chat")
    async def chat_history(limit: int = 200) -> JSONResponse:
        if kernel.chat is None:
            return JSONResponse({"messages": [], "busy": False})
        return JSONResponse({"messages": await kernel.chat.history(limit),
                             "busy": kernel.chat.busy})

    @app.post("/api/chat")
    async def chat_send(body: dict[str, Any]) -> JSONResponse:
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=422, detail="message required")
        try:
            result = await kernel.chat_message(message)
        except ChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (AgentError, ConfigError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.delete("/api/chat")
    async def chat_clear() -> JSONResponse:
        if kernel.chat is not None:
            await kernel.chat.clear()
        return JSONResponse({"ok": True})

    @app.get("/api/audit")
    async def audit(limit: int = 100) -> JSONResponse:
        records = await kernel.audit.tail(limit)
        return JSONResponse({"audit": [r.model_dump(mode="json") for r in records]})

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        # Websocket handshakes are exempt from the same-origin/CORS read
        # restrictions that protect fetch(), so a cross-site page could
        # otherwise open /ws and read the full live event bus (positions,
        # fills, AI decisions). Reject a browser Origin that is not ours,
        # independent of the bearer token.
        origin = ws.headers.get("origin")
        if origin is not None and not _origin_allowed(
                origin, ws.headers.get("host", "")):
            await ws.close(code=4403)
            return
        if auth_token and not _token_ok(ws.query_params.get("token")):
            await ws.close(code=4401)
            return
        await hub.handle(ws)

    app.include_router(terminal_router)
    terminal_bundle = STATIC_DIR / "terminal"
    if terminal_bundle.is_dir():  # bundle is committed; guard keeps bare
        app.mount("/terminal",    # checkouts (or stripped builds) booting
                  StaticFiles(directory=terminal_bundle, html=True),
                  name="terminal")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


class DashboardServer:
    def __init__(self, kernel: ApplicationKernel, *, host: str, port: int) -> None:
        config = uvicorn.Config(
            build_app(kernel), host=host, port=port,
            log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None
        self.host, self.port = host, port

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(), name="dashboard")
        log.info("dashboard listening", url=f"http://{self.host}:{self.port}")

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=5)
