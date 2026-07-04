"""Options income and hedging strategies.

These scan live chains for concrete candidate contracts (delta bands,
minimum premium yield, open-interest floors) and hand them to the AI as
evidence; multi-leg structures (spreads, condors) list every leg.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ...core.enums import OptionRight
from ...core.errors import DataError
from ...core.models import OptionContract
from ...data.router import DataRouter
from ...portfolio.state import PortfolioState
from ..base import Signal, Strategy


def _dte(contract: OptionContract) -> int:
    return (contract.expiration - datetime.now(UTC).date()).days


def _premium_yield(contract: OptionContract, spot: float, dte: int) -> float | None:
    if contract.bid is None or spot <= 0 or dte <= 0:
        return None
    return float(contract.bid) / spot * (365 / dte)


def _liquid(contract: OptionContract, min_oi: int) -> bool:
    return (contract.open_interest or 0) >= min_oi and contract.bid is not None and contract.ask is not None


def _in_delta_band(contract: OptionContract, low: float, high: float) -> bool:
    delta = contract.greeks.delta if contract.greeks else None
    return delta is not None and low <= abs(delta) <= high


class _OptionsStrategyBase(Strategy):
    async def _chain_and_spot(self, router: DataRouter,
                              symbol: str) -> tuple[list[OptionContract], float]:
        quote = await router.quote(symbol, allow_delayed=True)
        spot = float(quote.mid or quote.last or 0)
        if spot <= 0:
            raise DataError(f"no usable spot for {symbol}")
        chain = await router.option_chain(symbol, allow_delayed=True)
        candidates = [c for c in chain.contracts
                      if 15 <= _dte(c) <= int(self.options.get("max_dte", 60))]
        return candidates, spot


class CoveredCallStrategy(_OptionsStrategyBase):
    name = "covered_calls"
    description = "Sell OTM calls (delta 0.15-0.35) against long stock held in 100+ share lots."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        min_oi = int(self.options.get("min_open_interest", 200))
        min_yield = float(self.options.get("min_annualized_yield", 0.10))
        for position in portfolio.positions:
            if position.asset_class.value != "equity" or position.quantity < 100:
                continue
            symbol = position.symbol.upper()
            if self.symbols and symbol not in self.symbols:
                continue
            try:
                candidates, spot = await self._chain_and_spot(router, symbol)
            except DataError:
                continue
            calls = [c for c in candidates
                     if c.right is OptionRight.CALL and float(c.strike) > spot
                     and _liquid(c, min_oi) and _in_delta_band(c, 0.15, 0.35)]
            best: tuple[float, OptionContract] | None = None
            for c in calls:
                y = _premium_yield(c, spot, _dte(c))
                if y is not None and y >= min_yield and (best is None or y > best[0]):
                    best = (y, c)
            if best is not None:
                y, c = best
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="income",
                        strength=min(y / (max(min_yield, 1e-9) * 3), 1.0),
                        evidence={
                            "contract": c.symbol, "strike": str(c.strike),
                            "expiration": c.expiration.isoformat(), "dte": _dte(c),
                            "bid": str(c.bid), "delta": c.greeks.delta if c.greeks else None,
                            "open_interest": c.open_interest,
                            "annualized_yield": round(y, 4),
                            "shares_held": str(position.quantity),
                        },
                    )
                )
        return signals


class CashSecuredPutStrategy(_OptionsStrategyBase):
    name = "cash_secured_puts"
    description = "Sell OTM puts (delta 0.15-0.30) on watchlist names, fully cash-secured."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        min_oi = int(self.options.get("min_open_interest", 200))
        min_yield = float(self.options.get("min_annualized_yield", 0.12))
        cash = float(portfolio.account.cash) if portfolio.account else 0.0
        for symbol in self.symbols:
            try:
                candidates, spot = await self._chain_and_spot(router, symbol)
            except DataError:
                continue
            puts = [c for c in candidates
                    if c.right is OptionRight.PUT and float(c.strike) < spot
                    and _liquid(c, min_oi) and _in_delta_band(c, 0.15, 0.30)
                    and float(c.strike) * 100 <= cash]
            best: tuple[float, OptionContract] | None = None
            for c in puts:
                y = _premium_yield(c, float(c.strike), _dte(c))
                if y is not None and y >= min_yield and (best is None or y > best[0]):
                    best = (y, c)
            if best is not None:
                y, c = best
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="income",
                        strength=min(y / (max(min_yield, 1e-9) * 3), 1.0),
                        evidence={
                            "contract": c.symbol, "strike": str(c.strike),
                            "expiration": c.expiration.isoformat(), "dte": _dte(c),
                            "bid": str(c.bid), "delta": c.greeks.delta if c.greeks else None,
                            "open_interest": c.open_interest,
                            "annualized_yield_on_collateral": round(y, 4),
                            "collateral_required": str(Decimal(str(c.strike)) * 100),
                        },
                    )
                )
        return signals


class WheelStrategy(Strategy):
    name = "wheel"
    description = "The wheel: CSPs while flat, covered calls once assigned."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        csp = CashSecuredPutStrategy(symbols=self.symbols, options=self.options)
        cc = CoveredCallStrategy(symbols=self.symbols, options=self.options)
        held = {p.symbol.upper() for p in portfolio.positions if p.quantity >= 100}
        for signal in await csp.scan(router, portfolio):
            if signal.symbol not in held:
                signal.strategy = self.name
                signal.evidence["wheel_phase"] = "cash_secured_put"
                signals.append(signal)
        for signal in await cc.scan(router, portfolio):
            if signal.symbol in held and (not self.symbols or signal.symbol in self.symbols):
                signal.strategy = self.name
                signal.evidence["wheel_phase"] = "covered_call"
                signals.append(signal)
        return signals


class ProtectivePutStrategy(_OptionsStrategyBase):
    name = "protective_puts"
    description = "Hedge large single-name exposure with 0.25-0.40 delta puts."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        equity = float(portfolio.equity or 0)
        threshold = float(self.options.get("position_pct_trigger", 0.08))
        min_oi = int(self.options.get("min_open_interest", 100))
        if equity <= 0:
            return signals
        for position in portfolio.positions:
            value = float(position.market_value or 0)
            if position.asset_class.value != "equity" or value / equity < threshold:
                continue
            symbol = position.symbol.upper()
            try:
                candidates, spot = await self._chain_and_spot(router, symbol)
            except DataError:
                continue
            puts = [c for c in candidates
                    if c.right is OptionRight.PUT and _liquid(c, min_oi)
                    and _in_delta_band(c, 0.25, 0.40)]
            if not puts:
                continue
            best = min(puts, key=lambda c: float(c.ask or Decimal("1e9")))
            signals.append(
                Signal(
                    strategy=self.name, symbol=symbol, direction="hedge",
                    strength=min(value / equity / (max(threshold, 1e-9) * 2), 1.0),
                    evidence={
                        "position_pct_of_equity": round(value / equity, 3),
                        "contract": best.symbol, "strike": str(best.strike),
                        "expiration": best.expiration.isoformat(), "ask": str(best.ask),
                        "delta": best.greeks.delta if best.greeks else None,
                    },
                )
            )
        return signals


class VerticalSpreadStrategy(_OptionsStrategyBase):
    name = "vertical_spreads"
    description = "Defined-risk bull put spreads: sell ~0.30 delta put, buy the next strike down."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        min_oi = int(self.options.get("min_open_interest", 200))
        min_credit_ratio = float(self.options.get("min_credit_to_width", 0.25))
        for symbol in self.symbols:
            try:
                candidates, _spot = await self._chain_and_spot(router, symbol)
            except DataError:
                continue
            by_exp: dict[str, list[OptionContract]] = {}
            for c in candidates:
                if c.right is OptionRight.PUT and _liquid(c, min_oi):
                    by_exp.setdefault(c.expiration.isoformat(), []).append(c)
            for exp, puts in by_exp.items():
                puts.sort(key=lambda c: float(c.strike))
                shorts = [c for c in puts if _in_delta_band(c, 0.25, 0.35)]
                if not shorts:
                    continue
                short = shorts[-1]
                longs = [c for c in puts if float(c.strike) < float(short.strike)]
                if not longs:
                    continue
                long = longs[-1]
                if short.bid is None or long.ask is None:
                    continue
                credit = float(short.bid) - float(long.ask)
                width = float(short.strike) - float(long.strike)
                if width <= 0 or credit <= 0 or credit / width < min_credit_ratio:
                    continue
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="income",
                        strength=min(credit / width / (max(min_credit_ratio, 1e-9) * 2), 1.0),
                        evidence={
                            "structure": "bull_put_spread", "expiration": exp,
                            "short_leg": short.symbol, "long_leg": long.symbol,
                            "net_credit": round(credit, 2), "width": round(width, 2),
                            "credit_to_width": round(credit / width, 3),
                            "max_loss_per_spread": round((width - credit) * 100, 2),
                        },
                    )
                )
                break  # one candidate per symbol is enough context
        return signals


class IronCondorStrategy(_OptionsStrategyBase):
    name = "iron_condors"
    description = "Short ~0.20 delta strangle wrapped with protective wings (defined risk)."

    async def scan(self, router: DataRouter, portfolio: PortfolioState) -> list[Signal]:
        signals: list[Signal] = []
        min_oi = int(self.options.get("min_open_interest", 300))
        for symbol in self.symbols:
            try:
                candidates, _spot = await self._chain_and_spot(router, symbol)
            except DataError:
                continue
            by_exp: dict[str, list[OptionContract]] = {}
            for c in candidates:
                if _liquid(c, min_oi):
                    by_exp.setdefault(c.expiration.isoformat(), []).append(c)
            for exp, contracts in by_exp.items():
                puts = sorted([c for c in contracts if c.right is OptionRight.PUT],
                              key=lambda c: float(c.strike))
                calls = sorted([c for c in contracts if c.right is OptionRight.CALL],
                               key=lambda c: float(c.strike))
                short_puts = [c for c in puts if _in_delta_band(c, 0.15, 0.25)]
                short_calls = [c for c in calls if _in_delta_band(c, 0.15, 0.25)]
                if not short_puts or not short_calls:
                    continue
                sp, sc = short_puts[-1], short_calls[0]
                lp = next((c for c in reversed(puts) if float(c.strike) < float(sp.strike)), None)
                lc = next((c for c in calls if float(c.strike) > float(sc.strike)), None)
                if lp is None or lc is None:
                    continue
                if sp.bid is None or sc.bid is None or lp.ask is None or lc.ask is None:
                    continue
                credit = float(sp.bid) + float(sc.bid) - float(lp.ask) - float(lc.ask)
                if credit <= 0:
                    continue
                width = max(float(sp.strike) - float(lp.strike), float(lc.strike) - float(sc.strike))
                signals.append(
                    Signal(
                        strategy=self.name, symbol=symbol, direction="income",
                        strength=min(credit / width, 1.0) if width else 0.3,
                        evidence={
                            "structure": "iron_condor", "expiration": exp,
                            "legs": {"short_put": sp.symbol, "long_put": lp.symbol,
                                     "short_call": sc.symbol, "long_call": lc.symbol},
                            "net_credit": round(credit, 2),
                            "max_loss_per_condor": round((width - credit) * 100, 2),
                        },
                    )
                )
                break
        return signals
