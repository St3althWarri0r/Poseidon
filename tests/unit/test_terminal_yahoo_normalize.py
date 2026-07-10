"""Normalizers must reproduce lib/yahoo.ts field mapping and quirks."""

from __future__ import annotations

from poseidon.terminal.yahoo import (
    normalize_candles,
    normalize_fundamentals,
    normalize_news,
    normalize_quote,
    normalize_search,
)

QUOTE_KEYS = {
    "symbol", "name", "quoteType", "currency", "exchange", "marketState",
    "price", "change", "changePercent", "previousClose", "open", "dayHigh", "dayLow",
    "volume", "avgVolume", "marketCap", "trailingPE", "forwardPE", "eps",
    "dividendYield", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage",
    "twoHundredDayAverage", "sharesOutstanding", "postMarketPrice", "postMarketChange",
    "postMarketChangePercent", "preMarketPrice", "preMarketChange",
    "preMarketChangePercent",
}


def test_quote_shape_and_percent_quirk() -> None:
    q = normalize_quote({
        "symbol": "AAPL", "longName": "Apple Inc.", "quoteType": "EQUITY",
        "currency": "USD", "fullExchangeName": "NasdaqGS", "marketState": "REGULAR",
        "regularMarketPrice": 314.66, "regularMarketChange": 1.25,
        "regularMarketChangePercent": 0.4, "regularMarketVolume": 1000,
        "averageDailyVolume3Month": 2000, "dividendYield": 0.44, "trailingPE": 38.05,
    })
    assert set(q) == QUOTE_KEYS
    assert q["name"] == "Apple Inc."
    assert q["dividendYield"] == 0.0044  # percent -> fraction
    assert q["avgVolume"] == 2000
    assert q["eps"] is None  # absent -> null, key still present


def test_quote_name_fallback_chain() -> None:
    assert normalize_quote({"symbol": "X"})["name"] == "X"
    assert normalize_quote({"symbol": "X", "shortName": "Short"})["name"] == "Short"


def test_candles_drop_gaps_sort_and_dedupe_last_wins() -> None:
    result = {
        "timestamp": [30, 10, 10, 20],
        "indicators": {"quote": [{
            "open":  [3.0, 1.0, 1.5, None],
            "high":  [3.5, 1.2, 1.6, 2.2],
            "low":   [2.9, 0.9, 1.4, 1.9],
            "close": [3.2, 1.1, 1.5, 2.1],
            "volume": [None, 100, 150, 200],
        }]},
    }
    candles = normalize_candles(result)
    # ts=20 dropped (open null); ts=10 deduped last-wins (open 1.5); sorted asc.
    assert [c["time"] for c in candles] == [10, 30]
    assert candles[0]["open"] == 1.5
    assert candles[1]["volume"] == 0  # null volume -> 0


def test_news_normalization() -> None:
    items = normalize_news([
        {"uuid": "u1", "title": "T", "publisher": "P", "link": "https://x",
         "providerPublishTime": 1_700_000_000,
         "thumbnail": {"resolutions": [{"url": "https://img"}]},
         "relatedTickers": ["AAPL", 5]},
        {"title": "no link -> dropped"},
    ])
    assert len(items) == 1
    n = items[0]
    assert n["id"] == "u1" and n["publishedAt"] == 1_700_000_000_000
    assert n["thumbnail"] == "https://img" and n["tickers"] == ["AAPL", "5"]


def test_news_iso_publish_time() -> None:
    (n,) = normalize_news([{"title": "T", "link": "https://x",
                            "providerPublishTime": "2026-07-09T12:00:00Z"}])
    assert n["publishedAt"] == 1_783_598_400_000
    assert n["publisher"] == "—"


def test_search_optional_fields_omitted() -> None:
    out = normalize_search([
        {"symbol": "AAPL", "longname": "Apple", "exchDisp": "NASDAQ",
         "quoteType": "EQUITY", "sector": "Tech"},
        {"symbol": "ZZZ", "shortname": "Zed"},
        {"noSymbol": True},
    ])
    assert len(out) == 2
    assert out[0]["sector"] == "Tech" and "industry" not in out[0]
    assert "sector" not in out[1] and out[1]["type"] == "EQUITY"


def test_fundamentals_mapping_and_debt_quirk() -> None:
    f = normalize_fundamentals("AAPL", {
        "assetProfile": {"sector": "Technology", "fullTimeEmployees": 150000},
        "summaryDetail": {"marketCap": 3.1e12, "dividendYield": 0.0044,
                          "trailingPE": 38.05},
        "financialData": {"totalRevenue": 4.0e11, "debtToEquity": 79.55,
                          "grossMargins": 0.45},
        "defaultKeyStatistics": {"trailingEps": 7.1},
        "price": {"longName": "Apple Inc."},
    })
    assert set(f) == {"symbol", "profile", "valuation", "financials", "perShare", "targets"}
    assert f["profile"]["name"] == "Apple Inc."
    assert f["profile"]["employees"] == 150000
    assert f["valuation"]["marketCap"] == 3.1e12
    assert f["financials"]["debtToEquity"] == 0.7955  # percent -> ratio
    assert f["perShare"]["dividendYield"] == 0.0044   # already a fraction
    assert f["perShare"]["eps"] == 7.1
    assert f["targets"]["recommendationKey"] is None


def test_fundamentals_fallback_chains_pin() -> None:
    # ks values win only when sd/fd are absent...
    f = normalize_fundamentals("X", {
        "summaryDetail": {},
        "financialData": {},
        "defaultKeyStatistics": {"forwardPE": 21.0, "beta": 1.3, "profitMargins": 0.21},
    })
    assert f["valuation"]["forwardPE"] == 21.0
    assert f["valuation"]["beta"] == 1.3
    assert f["financials"]["profitMargins"] == 0.21
    # ...and the primary wins even when it is zero (?? semantics, not truthiness).
    f2 = normalize_fundamentals("X", {
        "summaryDetail": {"forwardPE": 0, "beta": 0.5},
        "financialData": {"profitMargins": 0.1},
        "defaultKeyStatistics": {"forwardPE": 9, "beta": 9, "profitMargins": 9},
    })
    assert f2["valuation"]["forwardPE"] == 0
    assert f2["valuation"]["beta"] == 0.5
    assert f2["financials"]["profitMargins"] == 0.1


def test_candles_ragged_arrays_are_safe() -> None:
    # Yahoo occasionally ships shorter indicator arrays than timestamps; rows
    # beyond an array's end are gaps, never IndexErrors.
    result = {
        "timestamp": [1, 2, 3],
        "indicators": {"quote": [{
            "open": [1.0], "high": [1.5, 2.5], "low": [0.5],
            "close": [1.2], "volume": [],
        }]},
    }
    candles = normalize_candles(result)
    assert [c["time"] for c in candles] == [1]
    assert candles[0]["volume"] == 0


def test_news_bool_publish_time_is_null() -> None:
    (n,) = normalize_news([{"title": "T", "link": "https://x",
                            "providerPublishTime": True}])
    assert n["publishedAt"] is None
