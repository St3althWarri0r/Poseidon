"""Attribution rollups: trade-level and contribution-level."""

from __future__ import annotations

from datetime import date

from poseidon.backtest.attribution import attribute_contributions, attribute_trades
from poseidon.backtest.engine import TradeRecord


def _t(symbol: str, entry: date, entry_price: float, qty: float,
       exit_date: date | None, exit_price: float | None, reason: str = "") -> TradeRecord:
    return TradeRecord(symbol=symbol, entry_date=entry, entry_price=entry_price,
                       quantity=qty, exit_date=exit_date, exit_price=exit_price,
                       reason=reason)


def test_attribute_trades_exact_rollups() -> None:
    d = date(2025, 1, 6)
    trades = [
        _t("AAPL", d, 100.0, 10, date(2025, 1, 10), 110.0, "take_profit"),   # +100, 4d
        _t("MSFT", d, 200.0, 5, date(2025, 1, 16), 190.0, "stop_loss"),      # -50, 10d
        _t("AAPL", date(2025, 2, 3), 100.0, 10, date(2025, 4, 10), 120.0,
           "time_stop"),                                                     # +200, 66d
        _t("NVDA", d, 50.0, 20, date(2025, 1, 31), 55.0, "take_profit"),     # +100, 25d
        _t("TSLA", d, 300.0, 2, date(2025, 1, 8), 290.0, "stop_loss"),       # -20, 2d
        _t("AMD", d, 80.0, 5, date(2025, 2, 14), 84.0, "time_stop"),         # +20, 39d
        _t("META", d, 400.0, 1, d, 405.0, "take_profit"),                    # +5, 0d
        _t("OPEN", d, 10.0, 1, None, None),                                  # open: excluded
    ]
    report = attribute_trades(trades, 100_000.0)

    assert [w["symbol"] for w in report["winners_top5"]] == [
        "AAPL", "AAPL", "NVDA", "AMD", "META"]
    assert report["winners_top5"][0]["pnl"] == 200.0
    assert report["winners_top5"][0]["reason"] == "time_stop"
    assert [w["symbol"] for w in report["losers_top5"]] == ["MSFT", "TSLA"]
    assert report["losers_top5"][0]["pnl"] == -50.0
    # total closed pnl 355; top5 winners sum 425 -> (355 - 425) / 100000
    assert report["return_ex_top5_winners"] == -0.0007
    assert report["by_exit_reason"] == {
        "stop_loss": {"trades": 2, "win_rate": 0.0, "total_pnl": -70.0},
        "take_profit": {"trades": 3, "win_rate": 1.0, "total_pnl": 205.0},
        "time_stop": {"trades": 2, "win_rate": 1.0, "total_pnl": 220.0},
    }
    assert report["by_symbol"]["AAPL"] == {"trades": 2, "total_pnl": 300.0, "win_rate": 1.0}
    assert report["by_symbol"]["MSFT"] == {"trades": 1, "total_pnl": -50.0, "win_rate": 0.0}
    assert "others" not in report["by_symbol"]
    assert list(report["by_symbol"]) == ["AAPL", "NVDA", "MSFT", "AMD", "TSLA", "META"]
    assert report["holding_day_buckets"] == {"0-5": 3, "6-20": 1, "21-60": 2, "60+": 1}


def test_attribute_trades_honesty_gate_below_three_closed() -> None:
    d = date(2025, 1, 6)
    trades = [
        _t("AAPL", d, 100.0, 10, date(2025, 1, 10), 110.0, "take_profit"),
        _t("MSFT", d, 200.0, 5, date(2025, 1, 16), 190.0, "stop_loss"),
        _t("OPEN", d, 10.0, 1, None, None),
        _t("OPEN2", d, 10.0, 1, None, None),
    ]
    report = attribute_trades(trades, 100_000.0)
    assert report == {"insufficient_trades": 2, "note_code": "need>=3_closed_trades"}


def test_attribute_contributions_exact() -> None:
    contrib = {"A": 1200.0, "B": -400.0, "C": 25.5}
    days_held = {"A": 50, "B": 10, "C": 3}
    report = attribute_contributions(contrib, days_held, 12.34, 100_000.0)
    assert report["winners_top5"] == [
        {"symbol": "A", "contribution": 1200.0, "days_held": 50},
        {"symbol": "C", "contribution": 25.5, "days_held": 3},
    ]
    assert report["losers_top5"] == [
        {"symbol": "B", "contribution": -400.0, "days_held": 10}]
    # total 825.5, top5 winners 1225.5 -> -400/100000
    assert report["return_ex_top5_winners"] == -0.004
    assert list(report["by_symbol"]) == ["A", "B", "C"]
    assert report["by_symbol"]["A"] == {"contribution": 1200.0, "days_held": 50}
    assert report["holding_day_buckets"] == {"0-5": 1, "6-20": 1, "21-60": 1, "60+": 0}
    assert report["trading_costs"] == 12.34


def test_attribute_contributions_caps_symbols_at_20_plus_others() -> None:
    contrib = {f"S{i:02d}": float(i + 1) * (1 if i % 2 else -1) for i in range(25)}
    days_held = dict.fromkeys(contrib, 1)
    report = attribute_contributions(contrib, days_held, 0.0, 100_000.0)
    symbols = [s for s in report["by_symbol"] if s != "others"]
    assert len(symbols) == 20
    # Ranked by |contribution| descending: magnitudes 25..6 survive, 5..1 pool.
    assert symbols[0] == "S24" and "S00" not in symbols
    assert report["by_symbol"]["others"] == {"count": 5, "contribution": -3.0}
    assert report["holding_day_buckets"] == {"0-5": 25, "6-20": 0, "21-60": 0, "60+": 0}
    # Conservation: rounded per-symbol contributions still sum to the total.
    total = sum(v["contribution"] for s, v in report["by_symbol"].items() if s != "others")
    total += report["by_symbol"]["others"]["contribution"]
    assert abs(total - sum(contrib.values())) < 0.01
