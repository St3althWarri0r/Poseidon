"""Tool dispatcher: every tool the AI can call is backed by live data.

There is no code path here that synthesizes market data. When a provider
chain fails, the tool result is an explicit error string and the model is
instructed to fold that into ``data_gaps`` and decline to trade on it.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import structlog

from ..core.config import RiskConfig
from ..core.errors import ConfigError, DataError
from ..data.router import DataRouter
from ..portfolio.state import PortfolioState
from ..risk.engine import RiskEngine
from ..strategy.workshop import AlgorithmWorkshop

log = structlog.get_logger(__name__)

_MAX_RESULT_CHARS = 60_000  # keep tool results bounded for the context window


class ToolDispatcher:
    def __init__(self, router: DataRouter, portfolio: PortfolioState, risk: RiskEngine,
                 *, allow_delayed_quotes: bool, benchmark_symbol: str = "SPY",
                 risk_config: RiskConfig | None = None,
                 workshop: AlgorithmWorkshop | None = None) -> None:
        self._router = router
        self._portfolio = portfolio
        self._risk = risk
        self._allow_delayed = allow_delayed_quotes
        self._benchmark = benchmark_symbol
        self._risk_config = risk_config or RiskConfig()
        self._workshop = workshop
        self.sources_used: set[str] = set()

    async def dispatch(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Execute a tool call. Returns (result_json, is_error)."""
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return json.dumps({"error": f"unknown tool {name}"}), True
            result = await handler(**tool_input)
            payload = json.dumps(result, default=str)
            if len(payload) > _MAX_RESULT_CHARS:
                payload = self._truncate(result)
            return payload, False
        except DataError as exc:
            log.warning("tool data error", tool=name, error=str(exc))
            return json.dumps({
                "error": str(exc),
                "instruction": "This data is unavailable live right now. Do not estimate it. "
                               "Record it in data_gaps and do not trade on assumptions.",
            }), True
        except TypeError as exc:
            return json.dumps({"error": f"bad arguments: {exc}"}), True
        except Exception as exc:
            log.exception("tool failed", tool=name)
            return json.dumps({"error": f"internal error: {exc}"}), True

    @staticmethod
    def _truncate(result: Any) -> str:
        if isinstance(result, dict):
            for key, value in result.items():
                if isinstance(value, list) and len(value) > 50:
                    result[key] = value[:50] + [f"... truncated {len(value) - 50} items"]
        return json.dumps(result, default=str)[:_MAX_RESULT_CHARS]

    # -- data tools --------------------------------------------------------------

    async def _tool_get_quote(self, symbol: str) -> dict[str, Any]:
        quote = await self._router.quote(symbol, allow_delayed=self._allow_delayed)
        self.sources_used.add(quote.source)
        return quote.model_dump(mode="json")

    async def _tool_get_bars(self, symbol: str, timeframe: str, limit: int) -> dict[str, Any]:
        bars = await self._router.bars(symbol, timeframe=timeframe, limit=limit)
        for b in bars[:1]:
            self.sources_used.add(b.source)
        return {"symbol": symbol.upper(), "timeframe": timeframe,
                "bars": [b.model_dump(mode="json") for b in bars]}

    async def _tool_get_option_chain(self, underlying: str,
                                     expiration: str | None) -> dict[str, Any]:
        exp = date.fromisoformat(expiration) if expiration else None
        chain = await self._router.option_chain(underlying, expiration=exp,
                                                allow_delayed=self._allow_delayed)
        self.sources_used.add(chain.source)
        return chain.model_dump(mode="json")

    async def _tool_get_news(self, symbols: list[str], limit: int) -> dict[str, Any]:
        articles = await self._router.news(symbols or None, limit=limit)
        for a in articles[:1]:
            self.sources_used.add(a.source)
        return {"articles": [a.model_dump(mode="json") for a in articles]}

    async def _tool_get_earnings_calendar(self, days_ahead: int,
                                          symbols: list[str]) -> dict[str, Any]:
        events = await self._router.earnings(days_ahead=days_ahead, symbols=symbols or None)
        for e in events[:1]:
            self.sources_used.add(e.source)
        return {"earnings": [e.model_dump(mode="json") for e in events]}

    async def _tool_get_economic_calendar(self, days_ahead: int) -> dict[str, Any]:
        events = await self._router.economic_calendar(days_ahead=days_ahead)
        for e in events[:1]:
            self.sources_used.add(e.source)
        return {"events": [e.model_dump(mode="json") for e in events]}

    # -- portfolio / risk tools -----------------------------------------------------

    async def _tool_get_portfolio(self) -> dict[str, Any]:
        state = self._portfolio.snapshot_dict()
        state["tax_lots"] = [lot.model_dump(mode="json") for lot in self._portfolio.tax_lots]
        state["recent_fills"] = [f.model_dump(mode="json") for f in self._portfolio.recent_fills[-20:]]
        state["dividends"] = [d.model_dump(mode="json") for d in self._portfolio.dividends[-20:]]
        return state

    async def _tool_get_risk_status(self) -> dict[str, Any]:
        return self._risk.status()

    # -- algorithm workshop ------------------------------------------------------

    async def _tool_list_algorithms(self) -> dict[str, Any]:
        if self._workshop is None:
            return {"algorithms": [], "note": "workshop not available in this context"}
        rows = await self._workshop.list_all()
        return {"algorithms": [
            {k: r[k] for k in ("id", "name", "description", "status", "created_by", "updated_at")}
            for r in rows
        ]}

    async def _tool_propose_algorithm(self, name: str, description: str, source: str,
                                      symbols: list[str]) -> dict[str, Any]:
        """Saved as a DRAFT — the operator reviews and activates on the
        dashboard. The AI can author algorithms but never arm them."""
        if self._workshop is None:
            return {"error": "workshop not available in this context"}
        try:
            record = await self._workshop.create(
                name=name, source=source, description=description,
                symbols=symbols or [], created_by="claude",
                review_notes="proposed during a review cycle",
            )
        except ConfigError as exc:
            return {"error": str(exc),
                    "instruction": "Fix the source to satisfy the validator and try again."}
        return {"saved": True, "id": record["id"], "name": record["name"], "status": "draft",
                "note": "Draft saved. The operator must activate it before it runs."}

    async def _tool_suggest_position_size(self, symbol: str) -> dict[str, Any]:
        """Vol-targeted size suggestion, from live quote + live bar history."""
        from ..analytics.sizing import daily_volatility, suggest_size

        quote = await self._router.quote(symbol, allow_delayed=self._allow_delayed)
        self.sources_used.add(quote.source)
        price = quote.mid or quote.last
        if price is None or price <= 0:
            raise DataError(f"no usable live price for {symbol}")
        bars = await self._router.bars(symbol, timeframe="1d", limit=60)
        vol = daily_volatility([float(b.close) for b in bars])
        if vol is None:
            raise DataError(f"not enough daily history to estimate {symbol} volatility")
        account = self._portfolio.account
        if account is None:
            raise DataError("no account snapshot — sync the portfolio first")
        result = suggest_size(
            equity=float(account.equity), price=float(price), daily_vol=vol,
            risk_budget_pct=self._risk_config.position_risk_budget_pct,
            max_position_pct=self._risk_config.max_position_pct,
            buying_power=float(account.buying_power),
        )
        result["symbol"] = symbol.upper()
        return result

    async def _tool_get_risk_metrics(self) -> dict[str, Any]:
        from ..analytics.risk_metrics import gather_risk_metrics

        cached = self._portfolio.risk_metrics
        age = self._portfolio.risk_metrics_age_seconds()
        if cached is not None and age is not None and age < 900:
            return dict(cached)
        report = await gather_risk_metrics(self._router, self._portfolio,
                                           benchmark=self._benchmark)
        payload = report.as_dict()
        self._portfolio.risk_metrics = payload
        self._portfolio.risk_metrics_at = report.as_of
        return payload
