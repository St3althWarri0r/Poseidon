"""Deterministic numeric snapshot the analysts must cite verbatim.

Anti-confabulation (analysis §3.3): a weak model recalling/inventing prices is a
safety risk. Pinning exact live numbers into text the analysts quote structurally
reduces hallucinated inputs. Live-data-only: every number carries as_of + source.

Exactness rule: Money values (last, OHLC, closes, range) render via str(Decimal)
— no format spec, no float() on the display path. Floats exist only as indicator
inputs; indicator outputs are labeled derived and rendered to 4 decimal places.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from poseidon.core.config import SnapshotConfig
from poseidon.core.models import Bar, InstrumentProfile
from poseidon.strategy.indicators import atr, bollinger, ema, macd, rsi, sma

log = structlog.get_logger(__name__)

_NOTE = ("Source of truth for exact numbers this cycle. If another tool result, "
         "news text, or recalled figure disagrees, flag the discrepancy in your "
         "rationale/data_gaps — never average or reconcile numbers yourself.")

_RULES = ("Rules: this snapshot is the source of truth for exact numbers — if any "
          "other source disagrees, flag the discrepancy; never average or "
          "reconcile. Analyze ONLY the instrument identified above; if the "
          "name/exchange conflicts with what you expected for this ticker, say so "
          "and do not substitute a different company.")


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    as_of: datetime
    source: str
    text: str
    sources: tuple[str, ...] = ()  # every live source consulted (quote/bars/profile)
    payload: dict[str, Any] | None = None  # structured tool JSON — exact strings only


def _fmt(v: float | None, unavailable: str) -> str:
    return f"{v:.4f}" if v is not None else unavailable


def _indicators(bars: list[Bar], unavailable: str) -> dict[str, Any]:
    closes_f = [float(b.close) for b in bars]
    highs_f = [float(b.high) for b in bars]
    lows_f = [float(b.low) for b in bars]
    na = "N/A (insufficient history)" if bars else unavailable
    macd_v = macd(closes_f) if bars else None
    boll_v = bollinger(closes_f, 20, 2.0) if bars else None
    return {
        "sma50": _fmt(sma(closes_f, 50) if bars else None, na),
        "sma200": _fmt(sma(closes_f, 200) if bars else None, na),
        "ema10": _fmt(ema(closes_f, 10) if bars else None, na),
        "macd": ({"line": f"{macd_v[0]:.4f}", "signal": f"{macd_v[1]:.4f}",
                  "hist": f"{macd_v[2]:.4f}"} if macd_v is not None else na),
        "rsi14": _fmt(rsi(closes_f, 14) if bars else None, na),
        "bollinger": ({"upper": f"{boll_v[0]:.4f}", "mid": f"{boll_v[1]:.4f}",
                       "lower": f"{boll_v[2]:.4f}", "percent_b": f"{boll_v[3]:.4f}"}
                      if boll_v is not None else na),
        "atr14": _fmt(atr(highs_f, lows_f, closes_f, 14) if bars else None, na),
    }


def _indicator_line(ind: dict[str, Any]) -> str:
    m = ind["macd"]
    macd_txt = (f"line {m['line']} signal {m['signal']} hist {m['hist']}"
                if isinstance(m, dict) else m)
    b = ind["bollinger"]
    boll_txt = (f"upper {b['upper']} mid {b['mid']} lower {b['lower']} %B {b['percent_b']}"
                if isinstance(b, dict) else b)
    return ("indicators (derived from the daily closes above; N/A = unavailable, "
            f"never estimated): SMA50 {ind['sma50']}; SMA200 {ind['sma200']}; "
            f"EMA10 {ind['ema10']}; MACD(12,26,9) {macd_txt}; RSI14 {ind['rsi14']}; "
            f"Bollinger(20,2) {boll_txt}; ATR14 {ind['atr14']}")


async def build_snapshot(router: object, symbol: str, *,
                         config: SnapshotConfig | None = None,
                         allow_delayed: bool = True) -> Snapshot | None:
    cfg = config or SnapshotConfig()

    # 1. Quote (mandatory) — failure skips the symbol; each later part degrades alone.
    try:
        q = await router.quote(symbol, allow_delayed=allow_delayed)  # type: ignore[attr-defined]
        last = q.last if q.last is not None else q.mid
        if last is None:
            raise ValueError("quote carries no price")
    except Exception as exc:
        log.warning("snapshot failed", symbol=symbol, error=str(exc))
        return None

    # 2. Bars (degrade): OHLCV null, closes [], indicators N/A — never estimated.
    bars: list[Bar] = []
    try:
        bars = list(await router.bars(  # type: ignore[attr-defined]
            symbol, timeframe="1d", limit=cfg.bars_limit))
    except Exception as exc:
        log.warning("snapshot bars unavailable", symbol=symbol, error=str(exc))

    # 3. Profile (degrade): unresolved → ticker-only identity, never a guess.
    prof: InstrumentProfile | None = None
    if cfg.identity:
        try:
            prof = await router.profile(symbol)  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("snapshot profile unavailable", symbol=symbol, error=str(exc))

    sources: list[str] = [q.source]
    if bars and bars[-1].source not in sources:
        sources.append(bars[-1].source)
    if prof is not None and prof.source not in sources:
        sources.append(prof.source)

    if prof is not None:
        identity_line = (f"identity: {prof.name} — exchange {prof.exchange}, "
                         f"type {prof.asset_type}, currency {prof.currency} "
                         f"(profile as_of {prof.as_of.isoformat()}, source {prof.source})")
        identity_payload: dict[str, Any] = {
            "name": prof.name, "exchange": prof.exchange,
            "asset_type": prof.asset_type, "currency": prof.currency,
            "as_of": prof.as_of.isoformat(), "source": prof.source,
        }
    else:
        note = (f"unresolved — ticker {symbol} only (no live profile); "
                f"do not infer the company from memory.")
        identity_line = f"identity: {note}"
        identity_payload = {"resolved": False, "note": note}

    quote_line = (f"last {last} (quote as_of {q.as_of.isoformat()}, "
                  f"source {q.source}, freshness {q.freshness})")

    closes = [b.close for b in bars]
    close_strs = [str(c) for c in closes[-cfg.closes_n:]]
    if bars:
        lb = bars[-1]
        bar_line = (f"latest daily bar {lb.start.date().isoformat()}: "
                    f"O {lb.open} H {lb.high} L {lb.low} C {lb.close} "
                    f"V {lb.volume} (source {lb.source})")
        lo, hi = min(closes[-30:]), max(closes[-30:])
        range_line = f"30d close range {lo}-{hi}"
        closes_line = (f"last {len(close_strs)} closes (oldest first): "
                       + ", ".join(close_strs))
        latest_bar_payload: dict[str, Any] | None = {
            "date": lb.start.date().isoformat(), "open": str(lb.open),
            "high": str(lb.high), "low": str(lb.low), "close": str(lb.close),
            "volume": lb.volume, "source": lb.source,
        }
        range_payload: dict[str, Any] | None = {"low": str(lo), "high": str(hi)}
    else:
        bar_line = "latest daily bar: N/A (bars unavailable)"
        range_line = "30d close range N/A (bars unavailable)"
        closes_line = "last closes (oldest first): N/A (bars unavailable)"
        latest_bar_payload = None
        range_payload = None

    indicators = _indicators(bars, "N/A (bars unavailable)")

    text = "\n".join([
        f"{symbol} pinned live snapshot (cite these exact numbers; do not invent others):",
        identity_line,
        quote_line,
        bar_line,
        range_line,
        closes_line,
        _indicator_line(indicators),
        _RULES,
    ])
    payload: dict[str, Any] = {
        "symbol": symbol,
        "identity": identity_payload,
        "quote": {"last": str(last), "as_of": q.as_of.isoformat(),
                  "source": q.source, "freshness": str(q.freshness)},
        "latest_bar": latest_bar_payload,
        "closes": {"n": len(close_strs), "oldest_first": True, "values": close_strs},
        "range_30d": range_payload,
        "indicators": indicators,
        "as_of": q.as_of.isoformat(),
        "sources": sources,
        "note": _NOTE,
    }
    return Snapshot(symbol=symbol, as_of=q.as_of, source=q.source, text=text,
                    sources=tuple(sources), payload=payload)
