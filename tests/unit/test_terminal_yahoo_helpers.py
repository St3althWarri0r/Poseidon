"""Pure helpers must mirror trading-terminal lib/yahoo.ts semantics."""

from __future__ import annotations

import pytest

from poseidon.terminal.yahoo import TTLCache, frac_from_pct, num, safe_sym, simplify_raw, text


def test_num_accepts_only_finite_numbers() -> None:
    assert num(3.5) == 3.5
    assert num(0) == 0
    assert num(True) is None  # bool is not a market number
    assert num(float("nan")) is None
    assert num(float("inf")) is None
    assert num("3.5") is None
    assert num(None) is None


def test_frac_from_pct() -> None:
    assert frac_from_pct(79.5) == 0.795
    assert frac_from_pct(None) is None


def test_text_and_safe_sym() -> None:
    assert text(None) == ""
    assert text("", "fb") == "fb"
    assert text("x", "fb") == "x"
    assert safe_sym(" aapl ") == "AAPL"
    assert safe_sym("^gspc") == "^GSPC"
    assert safe_sym("eurusd=x") == "EURUSD=X"
    assert safe_sym("BTC-USD;DROP") == "BTC-USDDROP"
    assert len(safe_sym("A" * 40)) == 20


def test_simplify_raw_unwraps_recursively() -> None:
    payload = {
        "marketCap": {"raw": 3.1e12, "fmt": "3.1T"},
        "profile": {"employees": {"raw": 150000, "fmt": "150k"}, "city": "Cupertino"},
        "plain": 7,
    }
    out = simplify_raw(payload)
    assert out == {"marketCap": 3.1e12, "profile": {"employees": 150000, "city": "Cupertino"},
                   "plain": 7}


async def test_ttl_cache_hits_within_ttl_and_refetches_after() -> None:
    cache = TTLCache()
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_fetch("k", 60.0, fetch) == 1
    assert await cache.get_or_fetch("k", 60.0, fetch) == 1  # cached
    assert calls == 1
    cache._store["k"] = (0.0, 1)  # force-expire
    assert await cache.get_or_fetch("k", 60.0, fetch) == 2


async def test_ttl_cache_does_not_cache_failures() -> None:
    cache = TTLCache()

    async def boom() -> int:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await cache.get_or_fetch("k", 60.0, boom)

    async def ok() -> int:
        return 42

    assert await cache.get_or_fetch("k", 60.0, ok) == 42


def test_cache_eviction_over_512() -> None:
    cache = TTLCache()
    for i in range(513):
        cache._store[f"k{i}"] = (0.0, i)  # all expired
    cache.evict_expired()
    assert len(cache._store) == 0
