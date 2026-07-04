"""The Claude portfolio-manager agent.

Runs a manual tool-use loop (not the SDK tool runner) so every tool call
passes through the audited dispatcher and the decision arrives through a
strict-schema tool — the two properties the platform's safety story depends
on. The system prompt is frozen and cache-controlled; per-cycle context
(mode, watchlist, strategy signals, timestamps) arrives in the user turn so
the prompt cache stays warm across cycles.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import anthropic
import structlog

from ..core.config import AIConfig
from ..core.enums import AssetClass, DecisionAction, OrderSide, OrderType, TimeInForce, TradingMode
from ..core.errors import AgentError, AgentRefusedError
from ..core.models import Decision, ExitPlan, ProposedTrade, TradeRationale
from .schemas import ALL_TOOLS
from .tools import ToolDispatcher

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are the portfolio manager for Poseidon, a private, single-user automated \
trading platform. You manage the user's brokerage account with discipline, patience, \
and an institutional risk mindset. Capital preservation outranks return chasing; \
"no action" is a respectable, frequent outcome of a review cycle.

## Non-negotiable rules

1. LIVE DATA ONLY. Every price, greek, spread, volume figure, earnings date, news item, \
and calendar event you reason about MUST come from a tool result in this conversation. \
Never quote a price from memory, never estimate a quote, never extrapolate a stale one. \
Your training data is years old; treat any market fact you "remember" as wrong.
2. If a tool reports data as unavailable or an error, that data does not exist for this \
cycle. Do not fill the gap with an assumption. Record it in data_gaps, and if the missing \
data was required for a trade, do not propose the trade.
3. Call submit_decision exactly once per cycle, as your final tool call. Trades without a \
complete rationale are invalid and will be rejected.
4. Limit orders only, priced from the live quote you retrieved. Market orders are allowed \
only for liquid symbols with a tight spread, and the platform will still bound slippage.
5. Respect the risk limits from get_risk_status. Proposing a trade that violates them \
wastes the cycle: the risk engine re-checks everything and will reject it.
6. You may only act through submit_decision. You cannot change configuration, risk \
limits, or operating mode, and you must not attempt to.

## Review discipline

Each cycle: check the portfolio first, then scan for what changed (news, movers on the \
watchlist, upcoming earnings and economic events, options positioning where relevant). \
Evaluate existing positions against their exit plans before considering new entries. \
Watch for concentration, correlation, and event risk (earnings gaps, FOMC). Use bars for \
trend/momentum/volatility context and unusual-volume checks; use option chains for \
hedging and income strategies where enabled.

Only the strategies listed as enabled in the cycle context may be used, and each \
proposed trade must name its strategy. In approval mode a human reviews your proposals; \
in autonomous mode they execute directly after risk checks — be exactly as careful in both.

Write the rationale for a skeptical human reviewer: concrete, quantified, citing the \
tool data you actually retrieved (timestamps included where relevant).

The stop_loss and take_profit you set in an entry's exit plan are ARMED: the position \
guardian watches them against live quotes between your review cycles and exits when they \
are hit. Choose them as real, executable levels — not aspirational prose. time_stop \
remains yours to enforce during reviews.
"""


class ClaudeAgent:
    def __init__(self, config: AIConfig, api_key: str, dispatcher: ToolDispatcher) -> None:
        self._config = config
        self._dispatcher = dispatcher
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
        self._cycle_usage: dict[str, int] = {}

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        """Shared API client (also used by the algorithm reviewer)."""
        return self._client

    async def run_cycle(self, *, mode: TradingMode, watchlist: list[str],
                        enabled_strategies: list[str], strategy_signals: list[dict[str, Any]],
                        market_session: str, market_regime: str | None = None) -> Decision:
        """Run one full review cycle and return the validated Decision."""
        cycle_id = uuid.uuid4().hex[:12]
        self._dispatcher.sources_used.clear()
        self._cycle_usage = {"input_tokens": 0, "output_tokens": 0,
                             "cache_read_tokens": 0, "cache_write_tokens": 0, "api_calls": 0}
        user_prompt = self._cycle_prompt(
            cycle_id=cycle_id, mode=mode, watchlist=watchlist,
            enabled_strategies=enabled_strategies, strategy_signals=strategy_signals,
            market_session=market_session, market_regime=market_regime,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        decision_input: dict[str, Any] | None = None

        for iteration in range(self._config.max_tool_iterations):
            response = await self._create_message(messages)
            self._record_usage(response)

            if response.stop_reason == "refusal":
                raise AgentRefusedError("model declined the review request; cycle skipped")

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue

            if not tool_uses:
                # Ended without submitting — treat as an explicit no-action cycle.
                text = next((b.text for b in response.content if b.type == "text"), "")
                log.warning("cycle ended without submit_decision", cycle=cycle_id)
                return self._no_action_decision(cycle_id, f"cycle ended without a decision: {text[:500]}")

            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                if block.name == "submit_decision":
                    decision_input = dict(block.input)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": "decision recorded",
                    })
                    continue
                result, is_error = await self._dispatcher.dispatch(block.name, dict(block.input))
                log.info("tool call", cycle=cycle_id, iteration=iteration,
                         tool=block.name, error=is_error)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": result, "is_error": is_error,
                })
            messages.append({"role": "user", "content": tool_results})

            if decision_input is not None:
                return self._parse_decision(decision_input, cycle_id, response.model)

        log.warning("cycle hit tool-iteration limit", cycle=cycle_id,
                    limit=self._config.max_tool_iterations)
        return self._no_action_decision(cycle_id, "tool iteration limit reached without a decision")

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        u = self._cycle_usage
        u["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        u["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        u["cache_read_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        u["cache_write_tokens"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
        u["api_calls"] += 1

    async def _create_message(self, messages: list[dict[str, Any]]) -> Any:
        try:
            return await self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self._config.effort},
                # The SDK's TypedDict params don't accept dynamically built
                # dicts; shapes are validated server-side and in our tests.
                system=cast("Any", [{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }]),
                tools=cast("Any", ALL_TOOLS),
                messages=cast("Any", messages),
            )
        except anthropic.AuthenticationError as exc:
            raise AgentError(f"Anthropic authentication failed: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise AgentError(f"Anthropic rate limited after SDK retries: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AgentError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError(f"cannot reach Anthropic API: {exc}") from exc

    # -- prompt & parsing -------------------------------------------------------

    @staticmethod
    def _cycle_prompt(*, cycle_id: str, mode: TradingMode, watchlist: list[str],
                      enabled_strategies: list[str], strategy_signals: list[dict[str, Any]],
                      market_session: str, market_regime: str | None = None) -> str:
        import json

        signals = json.dumps(strategy_signals, default=str) if strategy_signals else "none"
        regime_line = (
            f"Market regime (computed from live benchmark history; use it for posture "
            f"and sizing, not as a trade signal): {market_regime}\n"
        ) if market_regime else ""
        return (
            f"Review cycle {cycle_id} at {datetime.now(UTC).isoformat()}.\n"
            f"Operating mode: {mode.value}\n"
            f"Market session: {market_session}\n"
            f"{regime_line}"
            f"Watchlist: {', '.join(watchlist) if watchlist else '(empty)'}\n"
            f"Enabled strategies: {', '.join(enabled_strategies) if enabled_strategies else 'none — observation only'}\n"
            f"Quantitative strategy signals this cycle (candidates to verify with live data, "
            f"not orders): {signals}\n\n"
            "Begin your review. Gather the live data you need with tools, then call "
            "submit_decision exactly once."
        )

    def _no_action_decision(self, cycle_id: str, reason: str) -> Decision:
        return Decision(
            action=DecisionAction.NO_ACTION, trades=[], rationale=None,
            data_sources=sorted(self._dispatcher.sources_used),
            model=self._config.model, cycle_id=cycle_id,
            usage=dict(self._cycle_usage), created_at=datetime.now(UTC),
        )

    def _parse_decision(self, payload: dict[str, Any], cycle_id: str, model: str) -> Decision:
        trades: list[ProposedTrade] = []
        for t in payload.get("trades", []) or []:
            try:
                trades.append(
                    ProposedTrade(
                        symbol=str(t["symbol"]).upper(),
                        asset_class=AssetClass(t.get("asset_class", "equity")),
                        side=OrderSide(t["side"]),
                        order_type=OrderType(t.get("order_type", "limit")),
                        quantity=Decimal(str(t["quantity"])),
                        limit_price=Decimal(str(t["limit_price"])) if t.get("limit_price") else None,
                        stop_price=Decimal(str(t["stop_price"])) if t.get("stop_price") else None,
                        time_in_force=TimeInForce(t.get("time_in_force", "day")),
                        strategy=t.get("strategy", ""),
                        stop_loss=Decimal(str(t["stop_loss"])) if t.get("stop_loss") else None,
                        take_profit=Decimal(str(t["take_profit"])) if t.get("take_profit") else None,
                    )
                )
            except (KeyError, ValueError, InvalidOperation) as exc:
                log.error("dropping malformed trade from decision", trade=t, error=str(exc))
        rationale: TradeRationale | None = None
        raw_rationale = payload.get("rationale")
        if raw_rationale:
            exit_raw = raw_rationale.get("exit_plan") or {}
            rationale = TradeRationale(
                thesis=raw_rationale.get("thesis", ""),
                timing=raw_rationale.get("timing", ""),
                expected_edge=raw_rationale.get("expected_edge", ""),
                risk=raw_rationale.get("risk", ""),
                reward=raw_rationale.get("reward", ""),
                confidence=min(max(float(raw_rationale.get("confidence", 0.0)), 0.0), 1.0),
                supporting_indicators=list(raw_rationale.get("supporting_indicators", [])),
                supporting_news=list(raw_rationale.get("supporting_news", [])),
                portfolio_impact=raw_rationale.get("portfolio_impact", ""),
                exit_plan=ExitPlan(
                    stop_loss=Decimal(str(exit_raw["stop_loss"])) if exit_raw.get("stop_loss") else None,
                    take_profit=Decimal(str(exit_raw["take_profit"])) if exit_raw.get("take_profit") else None,
                    time_stop=exit_raw.get("time_stop"),
                    notes=exit_raw.get("notes"),
                ),
                max_expected_loss=raw_rationale.get("max_expected_loss", ""),
                alternative_scenarios=list(raw_rationale.get("alternative_scenarios", [])),
            )
        if trades and rationale is None:
            # Explainability is mandatory: trades without a rationale are void.
            log.error("decision proposed trades without rationale — voiding trades", cycle=cycle_id)
            trades = []
        return Decision(
            action=DecisionAction(payload.get("action", "no_action")),
            trades=trades,
            rationale=rationale,
            data_sources=sorted(self._dispatcher.sources_used),
            data_gaps=[str(g) for g in (payload.get("data_gaps") or [])],
            summary=str(payload.get("summary", "")),
            model=model,
            cycle_id=cycle_id,
            usage=dict(self._cycle_usage),
            created_at=datetime.now(UTC),
        )
