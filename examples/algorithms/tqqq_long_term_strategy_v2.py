# TQQQ Long Term Strategy V2 — faithful port of the Composer symphony
# 995Vk2yOZl6KUSZVDTWP (daily rebalance).
#
# Bull (SPY > its 200-day): hold TQQQ unless RSI(TQQQ,10)>79 or
# RSI(SPXL,10)>80 (overheated -> UVXY for the vol pop). Bear: buy
# capitulation (RSI(TQQQ,10)<31 -> TECL; RSI(SPY,10)<30 -> SPXL);
# otherwise the UVXY ladder: RSI(UVXY,10) in (74, 84] means the spike is
# still building -> hold UVXY; above 84 (blow-off) or below 74 (calm) ->
# TQQQ above its 20-day, else the better of SQQQ/BSV by RSI.

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

    async def trend_or_defense():
        if await above_sma("TQQQ", 20):
            return {"TQQQ": 1.0}
        a, b = await rsi("SQQQ", 10), await rsi("BSV", 10)
        if a is None or b is None:
            return {}
        return {("SQQQ" if a > b else "BSV"): 1.0}

    target = {}
    if await above_sma("SPY", 200):
        if (await rsi("TQQQ", 10) or 0) > 79 or (await rsi("SPXL", 10) or 0) > 80:
            target = {"UVXY": 1.0}
        else:
            target = {"TQQQ": 1.0}
    elif (await rsi("TQQQ", 10) or 100) < 31:
        target = {"TECL": 1.0}
    elif (await rsi("SPY", 10) or 100) < 30:
        target = {"SPXL": 1.0}
    else:
        uvxy = await rsi("UVXY", 10)
        if uvxy is not None and 74 < uvxy <= 84:
            target = {"UVXY": 1.0}
        else:
            target = await trend_or_defense()

    if not target:
        ctx.log("insufficient history for a target — emitting nothing")
        return []
    held = {p["symbol"].upper() for p in ctx.positions
            if float(p.get("quantity", 0) or 0) != 0}
    universe = {"TQQQ", "UVXY", "TECL", "SPXL", "SQQQ", "BSV"}
    signals = [{"symbol": s, "direction": "long", "strength": round(w, 3),
                "evidence": {"target_weight": round(w, 3),
                             "model": "TQQQ LTS V2 rebalance target"}}
               for s, w in target.items()]
    signals += [{"symbol": s, "direction": "exit", "strength": 0.9,
                 "evidence": {"reason": "no longer in the LTS V2 target"}}
                for s in sorted(held & universe - set(target))]
    return signals
