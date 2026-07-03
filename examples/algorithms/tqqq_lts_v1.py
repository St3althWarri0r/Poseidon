# TQQQ Long Term Strategy V1 — faithful port of the Composer symphony
# 3lc8Tb5NGu3cMZS2KLjj (daily rebalance).
#
# Tree: above TQQQ's own 200-day average, hold TQQQ unless RSI(TQQQ,10)
# blows past 79 (then flip to UVXY for the vol pop). Below the 200-day,
# buy capitulation (RSI(TQQQ,10)<31 -> TECL; RSI(SOXL,10)<30 -> SOXL);
# otherwise TQQQ above its 20-day, else the better of BSV/SQQQ by RSI.

async def scan(ctx):
    cache = {}

    async def closes(symbol):
        if symbol not in cache:
            bars = await ctx.bars(symbol, timeframe="1d", limit=260)
            cache[symbol] = [float(b.close) for b in bars]
        return cache[symbol]

    async def rsi(sym, w):
        return ctx.rsi(await closes(sym), w)

    async def above_sma(sym, w):
        series = await closes(sym)
        avg = ctx.sma(series, w)
        return avg is not None and series[-1] > avg

    target = {}
    if await above_sma("TQQQ", 200):
        overbought = await rsi("TQQQ", 10)
        target = {"UVXY": 1.0} if (overbought or 0) > 79 else {"TQQQ": 1.0}
    elif (await rsi("TQQQ", 10) or 100) < 31:
        target = {"TECL": 1.0}
    elif (await rsi("SOXL", 10) or 100) < 30:
        target = {"SOXL": 1.0}
    elif await above_sma("TQQQ", 20):
        target = {"TQQQ": 1.0}
    else:
        a, b = await rsi("BSV", 10), await rsi("SQQQ", 10)
        if a is not None and b is not None:
            target = {("BSV" if a > b else "SQQQ"): 1.0}

    if not target:
        ctx.log("insufficient history for a target — emitting nothing")
        return []

    held = {p["symbol"].upper() for p in ctx.positions
            if float(p.get("quantity", 0) or 0) != 0}
    universe = {"TQQQ", "UVXY", "TECL", "SOXL", "BSV", "SQQQ"}
    signals = [{"symbol": sym, "direction": "long", "strength": round(w, 3),
                "evidence": {"target_weight": round(w, 3),
                             "model": "TQQQ LTS V1 rebalance target"}}
               for sym, w in target.items()]
    signals += [{"symbol": sym, "direction": "exit", "strength": 0.9,
                 "evidence": {"reason": "no longer in the LTS V1 target"}}
                for sym in sorted(held & universe - set(target))]
    return signals
