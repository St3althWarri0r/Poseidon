# TQQQ Day Trader — intraday mean-reversion in the trend direction,
# written in the same regime-gated RSI style as the Long Term strategies.
#
# Unlike the daily-rebalance symphonies, this one works on 5-minute bars
# and re-evaluates EVERY review cycle. It enters as many times as its
# criteria trigger — zero on a quiet day, several on a whippy one — and
# flattens into the close. Pair it with a short review interval (60-300s)
# and a sleeve sized for 3x-ETF risk.
#
# Rules, in the family style:
#   Regime gate (daily): trade TQQQ when SPY > its 200-day, SQQQ when
#     below — always day-trading WITH the prevailing trend.
#   Entry (5m): RSI(14) of the instrument dips under 30 and the latest
#     5m close ticks back above the prior close (buy the turn, not the
#     falling knife), while the day's move in the instrument is not a
#     freefall (worse than -6% from the first visible 5m bar = stand
#     aside).
#   Exit (5m): RSI(14) recovers above 62, price loses the 20-bar average,
#     or the session is inside its final 20 minutes (no overnight 3x risk
#     from this book).

async def scan(ctx):
    from datetime import UTC, datetime

    async def daily_closes(symbol):
        bars = await ctx.bars(symbol, timeframe="1d", limit=260)
        return [float(b.close) for b in bars]

    spy = await daily_closes("SPY")
    spy_avg = ctx.sma(spy, 200)
    if spy_avg is None:
        ctx.log("not enough SPY history for the regime gate")
        return []
    instrument = "TQQQ" if spy[-1] > spy_avg else "SQQQ"

    intraday = await ctx.bars(instrument, timeframe="5m", limit=120)
    if len(intraday) < 30:
        ctx.log(f"not enough 5m bars for {instrument}")
        return []
    closes = [float(b.close) for b in intraday]
    rsi5m = ctx.rsi(closes, 14)
    sma20 = ctx.sma(closes, 20)
    if rsi5m is None or sma20 is None:
        return []

    session_open = closes[0]
    day_move = closes[-1] / session_open - 1.0 if session_open > 0 else 0.0

    held = any(p["symbol"].upper() == instrument and float(p.get("quantity", 0) or 0) > 0
               for p in ctx.positions)
    now = datetime.now(UTC)
    minutes_to_close = 20 * 60 - (now.hour * 60 + now.minute)  # 20:00 UTC ~ 4pm ET

    signals = []
    if held and (rsi5m > 62 or closes[-1] < sma20 or 0 < minutes_to_close <= 20):
        reason = ("5m RSI recovered" if rsi5m > 62
                  else "lost the 20-bar average" if closes[-1] < sma20
                  else "flattening into the close")
        signals.append({"symbol": instrument, "direction": "exit", "strength": 0.9,
                        "evidence": {"reason": reason, "rsi_5m": round(rsi5m, 1),
                                     "model": "TQQQ Day Trader"}})
    elif (not held and rsi5m < 30 and closes[-1] > closes[-2]
          and day_move > -0.06 and minutes_to_close > 45):
        signals.append({"symbol": instrument, "direction": "long", "strength": 0.8,
                        "evidence": {"target_weight": 1.0,
                                     "rsi_5m": round(rsi5m, 1),
                                     "day_move_pct": round(day_move * 100, 2),
                                     "setup": "oversold 5m flush turning up, with the daily trend",
                                     "model": "TQQQ Day Trader"}})
    return signals
