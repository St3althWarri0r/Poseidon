"""Read-only market-study endpoints backing the embedded terminal UI.

Thin handlers: validation + envelope only; data logic lives in yahoo.py.
Contract: trading-terminal's lib/types.ts. Always call through the module
(`yahoo.fn`) so tests and the UI harness can monkeypatch.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..core.errors import DataError
from . import yahoo
from .constants import RANGE_CONFIG

router = APIRouter(prefix="/api/terminal")


def _ok(data: Any, s_maxage: int) -> JSONResponse:
    return JSONResponse(data, headers={
        "Cache-Control":
            f"public, s-maxage={s_maxage}, stale-while-revalidate={s_maxage * 4}",
    })


def _fail(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.get("/quote")
async def quote(symbols: str = "") -> JSONResponse:
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if not syms:
        return _fail("Missing `symbols` query parameter", 400)
    if len(syms) > 60:
        return _fail("Too many symbols (max 60)", 400)
    try:
        return _ok(await yahoo.get_quotes(syms), 10)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/chart")
async def chart(symbol: str = "", range: str = "1M") -> JSONResponse:  # noqa: A002
    sym, range_key = symbol.strip(), range.upper()
    if not sym:
        return _fail("Missing `symbol` query parameter", 400)
    if range_key not in RANGE_CONFIG:
        return _fail(f"Invalid range: {range_key}", 400)
    try:
        return _ok(await yahoo.get_chart(sym, range_key),
                   30 if range_key in ("1D", "5D") else 120)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/search")
async def search(q: str = "") -> JSONResponse:
    if not q.strip():
        return _ok([], 60)
    try:
        return _ok(await yahoo.search_symbols(q), 60)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/fundamentals")
async def fundamentals(symbol: str = "") -> JSONResponse:
    if not symbol.strip():
        return _fail("Missing `symbol` query parameter", 400)
    try:
        return _ok(await yahoo.get_fundamentals(symbol), 3600)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/news")
async def news(symbol: str | None = None) -> JSONResponse:
    try:
        return _ok(await yahoo.get_news(symbol), 30)
    except DataError as exc:
        return _fail(str(exc), 502)


@router.get("/market")
async def market() -> JSONResponse:
    try:
        return _ok(await yahoo.get_market_overview(), 15)
    except DataError as exc:
        return _fail(str(exc), 502)
