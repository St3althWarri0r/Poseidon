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
from typing import Any

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

    def __getattr__(self, name: str) -> Any:  # quote / option_chain / news / ...
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

    async def run(self, strategy: Strategy, history: dict[str, list[Bar]],
                  *, start: date | None = None) -> BacktestResult:
        """Replay `history` (symbol -> chronological daily bars) through the
        strategy day by day. Days before `start` (if given) are warmup: the
        strategy's visibility advances but no trading or equity marking occurs."""
        config = self._config
        window = _HistoricalWindow({k.upper(): v for k, v in history.items()})
        router = _RouterShim(window)
        portfolio = _PortfolioShim()
        result = BacktestResult()

        all_dates = sorted({b.start.date() for bars in history.values() for b in bars})
        cash = config.starting_cash
        open_trades: list[TradeRecord] = []
        # True next-open execution: a signal/exit decided from day T's close can
        # only be filled at T+1's open (a live order cannot fill at the same
        # close that produced its signal). Buffering removes the same-bar fill
        # bias that otherwise credits breakout entries with the overnight gap.
        pending_entries: list[Signal] = []          # from yesterday's scan
        pending_exits: list[tuple[TradeRecord, str]] = []  # from yesterday's close
        last_close: dict[str, float] = {}
        bars_by_symbol_date: dict[tuple[str, date], Bar] = {
            (s.upper(), b.start.date()): b for s, bars in history.items() for b in bars
        }
        history_upper = {k.upper(): v for k, v in history.items()}

        def _equity() -> float:
            return cash + sum(
                t.quantity * last_close.get(t.symbol.upper(), t.entry_price) for t in open_trades
            )

        for day in all_dates:
            # Advance visibility cursors through today.
            for symbol, bars in history.items():
                count = sum(1 for b in bars if b.start.date() <= day)
                window.cursor[symbol.upper()] = count

            # Warmup: before `start`, advance visibility and last-known closes
            # only — no fills, no scans, no equity points.
            if start is not None and day < start:
                for symbol in history:
                    bar = bars_by_symbol_date.get((symbol.upper(), day))
                    if bar is not None:
                        last_close[symbol.upper()] = float(bar.close)
                continue

            # 1. Fill exits decided yesterday, at today's OPEN. If the symbol
            #    does not print today, carry the exit to the next session.
            carried_exits: list[tuple[TradeRecord, str]] = []
            for trade, reason in pending_exits:
                bar = bars_by_symbol_date.get((trade.symbol, day))
                if bar is None:
                    carried_exits.append((trade, reason))
                    continue
                price = float(bar.open) * (1 - config.slippage_pct)
                trade.exit_date, trade.exit_price, trade.reason = day, price, reason
                cash += trade.quantity * price - config.commission_per_trade
                result.trades.append(trade)
                if trade in open_trades:
                    open_trades.remove(trade)
            pending_exits = carried_exits

            # 2. Fill entries signalled yesterday, at today's OPEN. A signal that
            #    cannot fill today (symbol did not print) is stale and dropped.
            held = {t.symbol for t in open_trades}
            for signal in sorted(pending_entries, key=lambda s: s.strength, reverse=True):
                sym = signal.symbol.upper()
                if sym in held or len(open_trades) >= config.max_positions:
                    continue
                bar = bars_by_symbol_date.get((sym, day))
                if bar is None:
                    continue
                price = float(bar.open) * (1 + config.slippage_pct)
                budget = _equity() * config.position_pct
                if price <= 0 or budget + config.commission_per_trade > cash:
                    continue
                quantity = budget / price
                cash -= quantity * price + config.commission_per_trade
                open_trades.append(TradeRecord(symbol=sym, entry_date=day,
                                               entry_price=price, quantity=quantity))
                held.add(sym)
            pending_entries = []

            # 3. Update last-known closes and mark equity at today's CLOSE. A
            #    held symbol with no bar today is marked at its last close, not
            #    its entry price (which would fabricate a round-trip in returns).
            for symbol in history:
                bar = bars_by_symbol_date.get((symbol.upper(), day))
                if bar is not None:
                    last_close[symbol.upper()] = float(bar.close)
            result.equity_curve.append((day, _equity()))

            # 4. Decide exits from today's bar; fill next open. The stop is
            #    checked against today's LOW — the live guardian enforces stops
            #    intraday, so a close-only check would let a position trade far
            #    through its stop and recover, flattering the results. Targets
            #    and time stops are checked at the close (conservative).
            flagged = {id(t) for t, _ in pending_exits}
            for trade in open_trades:
                if id(trade) in flagged:
                    continue
                close = last_close.get(trade.symbol.upper())
                if close is None:
                    continue
                bar = bars_by_symbol_date.get((trade.symbol.upper(), day))
                low = float(bar.low) if bar is not None else close
                held_days = (day - trade.entry_date).days
                exit_reason: str | None = None
                if low <= trade.entry_price * (1 - config.stop_loss_pct):
                    exit_reason = "stop_loss"
                elif close >= trade.entry_price * (1 + config.take_profit_pct):
                    exit_reason = "take_profit"
                elif held_days >= config.max_hold_days:
                    exit_reason = "time_stop"
                if exit_reason:
                    pending_exits.append((trade, exit_reason))

            # 5. Scan today's data; buffer fresh long entries to fill next open.
            #    Capacity treats pending exits as vacated: they fill at the next
            #    open (step 1) before entries (step 2), so their slots are free
            #    by the time these buffered entries could fill. Step 2 still
            #    re-checks real capacity at fill time, so a carried exit that
            #    fails to fill cannot cause over-allocation.
            if len(open_trades) - len(pending_exits) < config.max_positions:
                signals = await strategy.scan(router, portfolio)  # type: ignore[arg-type]
                held = {t.symbol for t in open_trades}
                pending_entries = [s for s in signals
                                   if s.direction == "long" and s.symbol.upper() not in held]

        # Close remaining trades at the final visible price.
        for trade in open_trades:
            bars = history_upper.get(trade.symbol.upper()) or []
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
    # asyncio.run creates and tears down its own loop — robust on modern Python,
    # unlike get_event_loop() which is deprecated with no running loop and raises
    # when one is already running.
    return asyncio.run(
        strategy.scan(_RouterShim(window), _PortfolioShim())  # type: ignore[arg-type]
    )
