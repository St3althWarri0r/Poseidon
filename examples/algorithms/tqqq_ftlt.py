# TQQQ FTLT — faithful port of the Composer symphony
# "TQQQ Long Term Strategy Refined" (gVMVp8eQQIs6TgSmtI0r).
#
# Paste this into Poseidon's Algorithms editor (or POST /api/algorithms)
# and activate. Each review cycle it computes the symphony's target book
# and emits one signal per holding with `target_weight` in the evidence;
# it emits `exit` signals for held tickers that fell out of the target.
# In autonomous mode the AI executes the rebalance through the risk
# engine — no human input required.
#
# Honest deltas vs. Composer, unavoidable in any port:
#  * Composer rebalances at the close using close prints; Poseidon acts
#    when the cycle runs, at live prices.
#  * FNGU/BULZ are exchange-traded NOTES — confirm your broker carries
#    them or the top-N filters will select fewer names.
#  * UVXY/VIXY/3x ETFs decay; this strategy holds them deliberately and
#    briefly, but the drawdowns are violent. Size accordingly.

async def scan(ctx):
    cache = {}

    async def closes(symbol):
        if symbol not in cache:
            bars = await ctx.bars(symbol, timeframe="1d", limit=280)
            cache[symbol] = [float(b.close) for b in bars]
        return cache[symbol]

    async def rsi(sym, w):
        return ctx.rsi(await closes(sym), w)

    async def above_sma(sym, w):
        series = await closes(sym)
        avg = ctx.sma(series, w)
        return avg is not None and series[-1] > avg

    async def cumret(sym, w):
        return ctx.cumulative_return(await closes(sym), w)

    async def top_by(symbols, n, fn, w):
        scored = []
        for s in symbols:
            try:
                value = fn(await closes(s), w)
            except Exception:
                value = None
            if value is not None:
                scored.append((value, s))
        scored.sort(reverse=True)
        return [s for _, s in scored[:n]]

    async def tlt_race(winner, loser):
        a, b = await rsi("TLT", 10), await rsi("SQQQ", 10)
        if a is None or b is None:
            return {}
        return {winner: 1.0} if a > b else {loser: 1.0}

    def merge(target, block, scale):
        for sym, w in block.items():
            target[sym] = target.get(sym, 0.0) + w * scale

    async def sideways_deleverage():
        # Two 50/50 sub-blocks (as in the symphony).
        book = {}
        first = {"SPY": 1.0} if await above_sma("SPY", 20) else await tlt_race("QQQ", "PSQ")
        merge(book, first, 0.5)
        merge(book, await tlt_race("QQQ", "PSQ"), 0.5)
        return book

    async def crash_territory():
        if not await above_sma("QQQ", 20):
            cr60 = await cumret("QQQ", 60)
            if cr60 is not None and cr60 <= -12:
                return await sideways_deleverage()
            return await tlt_race("TQQQ", "SQQQ")
        if (await rsi("SQQQ", 10) or 100) < 31:
            return {"PSQ": 1.0}
        cr10 = await cumret("QQQ", 10)
        if cr10 is not None and cr10 > 5.5:
            return {"PSQ": 1.0}
        pick = await top_by(["QQQ", "SMH"], 1, ctx.rsi, 10)
        return {pick[0]: 1.0} if pick else {}

    async def non_crash():
        if not await above_sma("QQQ", 20):
            return await tlt_race("TQQQ", "SQQQ")
        if (await rsi("SQQQ", 10) or 100) < 31:
            return {"SQQQ": 1.0}
        cr70 = await cumret("QQQ", 70)
        if cr70 is not None and cr70 < -15:
            pick = await top_by(["TQQQ", "SOXL"], 1, ctx.rsi, 10)
            return {pick[0]: 1.0} if pick else {}
        picks = await top_by(["SPY", "QQQ", "DIA", "XLP"], 2, ctx.cumulative_return, 15)
        return {s: 1.0 / len(picks) for s in picks} if picks else {}

    async def half_b():
        if not await above_sma("QQQ", 20):
            cr60 = await cumret("QQQ", 60)
            if cr60 is not None and cr60 <= -12:
                return await sideways_deleverage()
            return await tlt_race("TQQQ", "SQQQ")
        return await non_crash()

    # ---------------- decision tree ----------------
    target = {}
    if await above_sma("SPY", 200):                       # Bull Market
        if (await rsi("QQQ", 10) or 0) > 80:
            target = {"UVXY": 1.0}
        elif (await rsi("SPY", 10) or 0) > 80:
            target = {"UVXY": 1.0}
        elif (await rsi("SPY", 60) or 0) > 60:
            pick = await top_by(["VIXY", "TMF"], 1, ctx.moving_average_return, 15)
            target = {pick[0]: 1.0} if pick else {}
        else:
            picks = await top_by(["TQQQ", "SOXL", "TECL", "UDOW", "UPRO", "FNGU", "BULZ"],
                                 3, ctx.moving_average_return, 21)
            target = {s: 1.0 / len(picks) for s in picks} if picks else {}
    else:                                                 # Dip Buy Strategy
        if (await rsi("TQQQ", 10) or 100) < 31:
            target = {"TECL": 1.0}
        elif (await rsi("SMH", 10) or 100) < 30:
            target = {"SOXL": 1.0}
        elif (await rsi("FNGS", 10) or 100) < 30:
            target = {"FNGU": 1.0}
        elif (await rsi("SPY", 10) or 100) < 30:
            target = {"UPRO": 1.0}
        else:                                             # Bear/Sideways, 50/50 books
            cr252 = await cumret("QQQ", 252)
            book_a = await (crash_territory() if (cr252 is not None and cr252 < -20)
                            else non_crash())
            merge(target, book_a, 0.5)
            merge(target, await half_b(), 0.5)

    if not target:
        ctx.log("no target computed (insufficient history) — emitting nothing")
        return []

    held = {p["symbol"].upper() for p in ctx.positions
            if float(p.get("quantity", 0) or 0) != 0}
    universe = {"TQQQ", "SOXL", "TECL", "UDOW", "UPRO", "FNGU", "BULZ", "UVXY",
                "VIXY", "TMF", "SQQQ", "PSQ", "QQQ", "SPY", "SMH", "DIA", "XLP"}
    signals = []
    for sym, weight in sorted(target.items(), key=lambda kv: -kv[1]):
        signals.append({"symbol": sym, "direction": "long",
                        "strength": round(min(weight, 1.0), 3),
                        "evidence": {"target_weight": round(weight, 3),
                                     "model": "TQQQ FTLT rebalance target"}})
    for sym in sorted(held & universe - set(target)):
        signals.append({"symbol": sym, "direction": "exit", "strength": 0.9,
                        "evidence": {"reason": "no longer in the FTLT target book"}})
    return signals
