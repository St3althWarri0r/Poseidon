"""Dashboard API server.

Binds to localhost only by default. There is deliberately no remote
authentication story here — the dashboard is a local desktop surface; if
you must access it remotely, put it behind an authenticated reverse proxy
or an SSH tunnel (docs/security.md).
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.enums import TradingMode
from ..core.events import EventBus

if TYPE_CHECKING:
    from ..app import ApplicationKernel

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class WebsocketHub:
    """Fan out every bus event to connected dashboard clients."""

    def __init__(self, bus: EventBus) -> None:
        self._clients: set[WebSocket] = set()
        bus.subscribe("*", self._on_event)

    async def _on_event(self, topic: str, payload: Any) -> None:
        if not self._clients:
            return
        message = json.dumps({"topic": topic, "payload": payload}, default=str)
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
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

    # Optional bearer-token auth (required by config validation whenever the
    # host is non-loopback). Static assets are exempt — they contain nothing
    # sensitive and <link>/<script> tags cannot carry headers.
    auth_token: str | None = None
    if kernel.config.dashboard.auth_token_credential:
        auth_token = kernel.vault.get(kernel.config.dashboard.auth_token_credential)

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
            if request.url.path.startswith("/static"):
                return await call_next(request)
            supplied = request.query_params.get("token")
            header = request.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                supplied = header.removeprefix("Bearer ")
            if not _token_ok(supplied):
                return StarletteJSON({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

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
        rows = await kernel.db.fetch_all(
            "SELECT at, equity FROM equity_marks ORDER BY at DESC LIMIT ?", (limit,)
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
        approve = bool(body.get("approve"))
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
        asyncio.create_task(kernel.run_review_cycle())
        return JSONResponse({"ok": True, "detail": "review cycle started"})

    @app.get("/api/performance")
    async def performance() -> JSONResponse:
        return JSONResponse(await kernel.performance_report())

    @app.get("/api/exit-plans")
    async def exit_plans() -> JSONResponse:
        return JSONResponse({"plans": await kernel.guardian.active_plans()})

    @app.get("/api/quote/{symbol}")
    async def quote(symbol: str) -> JSONResponse:
        """Live quote for the trade ticket (delayed data never quoted here —
        a human about to trade sees the same freshness bar as the AI)."""
        try:
            q = await kernel.router.quote(symbol.upper(), allow_delayed=False)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(q.model_dump(mode="json"))

    @app.post("/api/trade")
    async def trade(body: dict[str, Any]) -> JSONResponse:
        """Manual order entry. Same pipeline as AI orders: full risk engine,
        duplicate guard, broker preflight, lifecycle polling, audit."""
        from decimal import Decimal, InvalidOperation

        from ..core.enums import AssetClass, OrderSide, OrderType, TimeInForce
        from ..core.models import Order

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
                extended_hours=bool(body.get("extended_hours", False)),
                strategy="manual",
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise HTTPException(status_code=422, detail=f"invalid order: {exc}") from exc
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.limit_price is None:
            raise HTTPException(status_code=422, detail="limit orders need a limit_price")
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
            )
        except CfgErr as exc:
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
        try:
            record = await kernel.workshop.deactivate(
                algo_id, archive=bool((body or {}).get("archive", False))
            )
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

    @app.get("/api/audit")
    async def audit(limit: int = 100) -> JSONResponse:
        records = await kernel.audit.tail(limit)
        return JSONResponse({"audit": [r.model_dump(mode="json") for r in records]})

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        if auth_token and not _token_ok(ws.query_params.get("token")):
            await ws.close(code=4401)
            return
        await hub.handle(ws)

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
