"""Position sizing must work at any account size, on any asset.

Observed in production on a $42.1M paper account: the autonomous trader stopped
placing crypto orders entirely. Two independent defects in `suggest_size`, at
opposite ends of the account-size range:

* **Large accounts** — nothing reconciled `max_position_pct` with the broker's
  PER-ORDER cap. 20% of $42.1M is $8.4M against Alpaca's $200k crypto cap, i.e.
  42x unplaceable. The model (correctly, since #36 puts the cap in its prompt)
  saw the conflict and declined to trade rather than size down, so nothing
  traded at all.
* **Small accounts** — `int(...)` truncation. A $100 account sizing BTC at
  $63,319 got **0**, because any fraction of a share floors to zero. Crypto is
  inherently fractional; whole-share truncation makes every asset priced above
  the account balance untradeable.

Both are sizing bugs, not configuration. "Reset your account to $100k" is a
workaround, not a fix: an autonomous trader has to work at $100 and at $100
billion.
"""

from __future__ import annotations

from poseidon.analytics.sizing import suggest_size

BTC = 63_319.0
CAP = 200_000.0          # alpaca's per-order crypto notional cap


def _size(**kw: float | bool | None) -> dict:
    base: dict = {"price": BTC, "daily_vol": 0.03, "risk_budget_pct": 0.005,
                  "max_position_pct": 0.20}
    base.update(kw)
    base.setdefault("buying_power", float(base["equity"]) * 4)  # type: ignore[arg-type]
    return suggest_size(**base)  # type: ignore[arg-type]


# -- large accounts: respect the broker's per-order cap -------------------------

def test_large_account_is_capped_to_the_brokers_per_order_limit() -> None:
    r = _size(equity=42_122_829.79, max_order_notional=CAP, fractional=True)
    assert r["notional"] <= CAP, (
        f"sized {r['notional']} against a {CAP} per-order cap — the broker will "
        "refuse this, and the model then declines to trade at all"
    )
    assert r["suggested_shares"] > 0, "a capped size must still be tradeable"
    assert any("per-order" in c for c in r["capped_by"]), r["capped_by"]


def test_the_cap_is_reported_so_the_model_can_plan_across_orders() -> None:
    r = _size(equity=42_122_829.79, max_order_notional=CAP, fractional=True)
    assert "capped_by" in r and r["capped_by"]
    # position-size cap is NOT the binding one here; the broker cap is
    assert any("per-order" in c for c in r["capped_by"])


def test_without_a_cap_behaviour_is_unchanged() -> None:
    """Brokers that declare no per-order limit must size exactly as before."""
    before = _size(equity=1_000_000.0)
    after = _size(equity=1_000_000.0, max_order_notional=None)
    assert before["suggested_shares"] == after["suggested_shares"]


# -- small accounts: fractional assets must not floor to zero ------------------

def test_small_account_can_still_buy_a_fraction_of_an_expensive_asset() -> None:
    r = _size(equity=100.0, buying_power=100.0, fractional=True)
    assert r["suggested_shares"] > 0, (
        "a $100 account sized 0 BTC — int() truncation makes every asset priced "
        "above the balance untradeable"
    )
    assert r["notional"] <= 100.0


def test_whole_share_assets_still_floor_to_integers() -> None:
    """Equities are not fractional by default — this must not start emitting
    3.7 shares of AAPL."""
    r = _size(equity=100_000.0, price=150.0, fractional=False)
    assert float(r["suggested_shares"]).is_integer()


def test_fractional_sizing_floors_rather_than_rounds() -> None:
    """Rounding UP could breach the very cap we just applied."""
    r = _size(equity=42_122_829.79, max_order_notional=CAP, fractional=True)
    assert r["suggested_shares"] * BTC <= CAP


# -- the range, end to end -----------------------------------------------------

def test_sizing_is_sane_across_six_orders_of_magnitude() -> None:
    for equity in (100.0, 10_000.0, 1_000_000.0, 42_122_829.79, 100_000_000_000.0):
        r = _size(equity=equity, max_order_notional=CAP, fractional=True)
        assert r["suggested_shares"] > 0, f"no tradeable size at equity={equity}"
        assert r["notional"] <= CAP + 1e-6, f"cap breached at equity={equity}"
        assert r["notional"] <= equity * 0.20 + 1e-6, f"position cap breached at {equity}"
