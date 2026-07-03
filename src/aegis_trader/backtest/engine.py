"""Backtesting engine.

Replays historical daily bars through the *same* strategy classes used
live (they only see data up to the simulated day — no lookahead), applies
simple next-open execution with configurable slippage and commission, and
produces an equity curve plus summary statistics.

This engine tests the quantitative screeners. The AI judgment layer is not
simulated — Claude's decisions depend on live news/calendars that do not
exist historically; pretending otherwise would be the kind of fabrication
this platform bans. Use paper trading (Mode: paper broker) to evaluate the
full AI loop forward in time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..core.models import Bar
from ..strategy.base import Signal, Strategy


@dataclass
class BacktestConfig:
    starting_cash: float = 100_000.0
    position_pct: float = 0.10        # of equity per entry
    max_positions: int = 10
    slippage_pct: float = 0.0005
    commission_per_trade: float = 0.0
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.20
    max_hold_days: int = 60


@dataclass
class TradeRecord:
    symbol: str
    entry_date: date
    entry_price: float
    quantity: float
    exit_date: date | None = None
    exit_price: float | None = None
    reason: str = ""

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.quantity


@dataclass
class BacktestResult:
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)

    @property
    def daily_returns(self) -> list[float]:
        curve = self.equity_curve
        return [curve[i][1] / curve[i - 1][1] - 1 for i in range(1, len(curve))
                if curve[i - 1][1] > 0]

    @property
    def total_return(self) -> float:
        if len(self.equity_curve) < 2 or self.equity_curve[0][1] == 0:
            return 0.0
        return self.equity_curve[-1][1] / self.equity_curve[0][1] - 1

    @property
    def max_drawdown(self) -> float:
        peak, worst = float("-inf"), 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak)
        return worst

    @property
    def sharpe(self) -> float:
        rets = self.daily_returns
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = var ** 0.5
        return (mean / std) * (252 ** 0.5) if std > 0 else 0.0

    @property
    def win_rate(self) -> float:
        closed = [t for t in self.trades if t.exit_price is not None]
        if not closed:
            return 0.0
        return sum(1 for t in closed if t.pnl > 0) / len(closed)

    def summary(self) -> dict[str, float | int]:
        return {
            "total_return": round(self.total_return, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe": round(self.sharpe, 2),
            "trades": len(self.trades),
            "win_rate": round(self.win_rate, 3),
            "final_equity": round(self.equity_curve[-1][1], 2) if self.equity_curve else 0,
        }


class _HistoricalWindow:
    """Serves each strategy only the bars visible on the simulated day —
    the anti-lookahead guarantee."""

    def __init__(self, history: dict[str, list[Bar]]) -> None:
        self._history = history
        self.cursor: dict[str, int] = {}

    def visible_bars(self, symbol: str, limit: int) -> list[Bar]:
        bars = self._history.get(symbol.upper(), [])
        end = self.cursor.get(symbol.upper(), 0)
        return bars[max(0, end - limit):end]


class _RouterShim:
    """Duck-typed stand-in for DataRouter: strategies only call .bars in
    backtests (quote/chain access raises, keeping option strategies honest
    about needing live data)."""

    def __init__(self, window: _HistoricalWindow) -> None:
        self._window = window

    async def bars(self, symbol: str, *, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        if timeframe != "1d":
            raise ValueError("backtests replay daily bars")
        bars = self._window.visible_bars(symbol, limit)
        if not bars:
            from ..core.errors import DataError

            raise DataError(f"no history for {symbol} at this point in the replay")
        return bars

    def __getattr__(self, name: str):  # quote / option_chain / news / ...
        from ..core.errors import DataError

        async def unavailable(*_args: object, **_kwargs: object) -> None:
            raise DataError(f"'{name}' is not available in historical replay")

        return unavailable


class _PortfolioShim:
    """Minimal PortfolioState stand-in for strategies during replay."""

    def __init__(self) -> None:
        self.positions: list[object] = []
        self.account = None
        self.equity = None

    def position_for(self, _symbol: str) -> None:
        return None


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self._config = config or BacktestConfig()

    async def run(self, strategy: Strategy, history: dict[str, list[Bar]]) -> BacktestResult:
        """Replay `history` (symbol -> chronological daily bars) through the
        strategy day by day."""
        config = self._config
        window = _HistoricalWindow({k.upper(): v for k, v in history.items()})
        router = _RouterShim(window)
        portfolio = _PortfolioShim()
        result = BacktestResult()

        all_dates = sorted({b.start.date() for bars in history.values() for b in bars})
        cash = config.starting_cash
        open_trades: list[TradeRecord] = []
        bars_by_symbol_date: dict[tuple[str, date], Bar] = {
            (s.upper(), b.start.date()): b for s, bars in history.items() for b in bars
        }

        for day in all_dates:
            # Advance visibility cursors through today.
            for symbol, bars in history.items():
                count = sum(1 for b in bars if b.start.date() <= day)
                window.cursor[symbol.upper()] = count

            # Manage exits at today's close.
            still_open: list[TradeRecord] = []
            for trade in open_trades:
                bar = bars_by_symbol_date.get((trade.symbol, day))
                if bar is None:
                    still_open.append(trade)
                    continue
                close = float(bar.close)
                held = (day - trade.entry_date).days
                exit_reason = None
                if close <= trade.entry_price * (1 - config.stop_loss_pct):
                    exit_reason = "stop_loss"
                elif close >= trade.entry_price * (1 + config.take_profit_pct):
                    exit_reason = "take_profit"
                elif held >= config.max_hold_days:
                    exit_reason = "time_stop"
                if exit_reason:
                    price = close * (1 - config.slippage_pct)
                    trade.exit_date, trade.exit_price, trade.reason = day, price, exit_reason
                    cash += trade.quantity * price - config.commission_per_trade
                    result.trades.append(trade)
                else:
                    still_open.append(trade)
            open_trades = still_open

            # Mark equity.
            equity = cash
            for trade in open_trades:
                bar = bars_by_symbol_date.get((trade.symbol, day))
                mark = float(bar.close) if bar else trade.entry_price
                equity += trade.quantity * mark
            result.equity_curve.append((day, equity))

            # New entries from today's signals, executed at today's close
            # with slippage (signals computed on data through today).
            if len(open_trades) >= config.max_positions:
                continue
            signals = await strategy.scan(router, portfolio)  # type: ignore[arg-type]
            held = {t.symbol for t in open_trades}
            for signal in sorted(signals, key=lambda s: s.strength, reverse=True):
                if signal.direction != "long" or signal.symbol in held:
                    continue
                if len(open_trades) >= config.max_positions:
                    break
                bar = bars_by_symbol_date.get((signal.symbol, day))
                if bar is None:
                    continue
                price = float(bar.close) * (1 + config.slippage_pct)
                budget = equity * config.position_pct
                if budget > cash or price <= 0:
                    continue
                quantity = budget / price
                cash -= quantity * price + config.commission_per_trade
                trade = TradeRecord(symbol=signal.symbol, entry_date=day,
                                    entry_price=price, quantity=quantity)
                open_trades.append(trade)
                held.add(signal.symbol)

        # Close remaining trades at the final visible price.
        for trade in open_trades:
            bars = history.get(trade.symbol) or history.get(trade.symbol.upper()) or []
            if bars:
                trade.exit_date = bars[-1].start.date()
                trade.exit_price = float(bars[-1].close)
                trade.reason = "end_of_data"
            result.trades.append(trade)
        return result


def signals_only_replay(strategy: Strategy, history: dict[str, list[Bar]],
                        on_day: date) -> list[Signal]:
    """Utility for tests: what would the strategy have signaled on a given day."""
    import asyncio

    window = _HistoricalWindow({k.upper(): v for k, v in history.items()})
    for symbol, bars in history.items():
        window.cursor[symbol.upper()] = sum(1 for b in bars if b.start.date() <= on_day)
    return asyncio.get_event_loop().run_until_complete(
        strategy.scan(_RouterShim(window), _PortfolioShim())  # type: ignore[arg-type]
    )
