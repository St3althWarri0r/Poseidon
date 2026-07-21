"""SEC EDGAR fundamentals & filings provider (https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

Keyless: EDGAR's data APIs need no key, only an identified ``User-Agent`` per
the SEC fair-access policy — set ``data.providers[].options.user_agent`` to
your contact (e.g. ``"your-name your@email"``). Requests are politely paced
(min 0.2s monotonic gap under an asyncio lock — far below the 10 req/s
ceiling).

Capabilities:

  * FUNDAMENTALS — ``companyfacts`` XBRL reduced immediately to a small
    curated concept map (the raw JSON is multi-MB for large filers and full of
    tag drift; fallback tag lists absorb the common variants). Values are
    served verbatim as Decimals with their fiscal/filed dates so downstream
    consumers — including the point-in-time research slice — can reason about
    when each figure became knowable. The overview carries the entity name
    ONLY: EDGAR files no ratios and none are ever fabricated.
  * FILINGS — ``submissions`` recent-filings metadata (form, dates, items,
    document link). Metadata + links only, never fetched document text.

Ticker→CIK resolution uses the bundled ``company_tickers.json`` mapping,
cached in-provider for 24h; an unknown ticker is a non-retryable error (the
symbol has no EDGAR presence — retrying cannot fix it).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from ... import __version__
from ...core.errors import ProviderError
from ...core.models import Filing, FundamentalsOverview, FundamentalsReport, StatementPeriod
from ..base import DataCapability, MarketDataProvider

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

_MIN_REQUEST_GAP = 0.2  # seconds — polite pacing, well under SEC's 10 req/s cap
_TICKER_CACHE_TTL = 24 * 3600.0  # the ticker->CIK map changes on listing timescales

_ACCEPTED_FORMS = frozenset({"10-K", "10-Q"})
_MAX_PERIODS = 8  # newest statement periods kept after curation

# Curated concept map: canonical item -> (taxonomy, fallback tags in preference
# order, exact unit key). USD-family units only; tag lists absorb the known
# XBRL tag drift (modern revenue tag vs legacy Revenues/SalesRevenueNet, etc.).
_CONCEPTS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("revenue", "us-gaap",
     ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
      "SalesRevenueNet"), "USD"),
    ("net_income", "us-gaap", ("NetIncomeLoss",), "USD"),
    ("gross_profit", "us-gaap", ("GrossProfit",), "USD"),
    ("operating_income", "us-gaap", ("OperatingIncomeLoss",), "USD"),
    ("total_assets", "us-gaap", ("Assets",), "USD"),
    ("total_liabilities", "us-gaap", ("Liabilities",), "USD"),
    ("shareholder_equity", "us-gaap",
     ("StockholdersEquity",
      "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), "USD"),
    ("cash_and_equivalents", "us-gaap", ("CashAndCashEquivalentsAtCarryingValue",), "USD"),
    ("long_term_debt", "us-gaap", ("LongTermDebtNoncurrent", "LongTermDebt"), "USD"),
    ("operating_cashflow", "us-gaap",
     ("NetCashProvidedByUsedInOperatingActivities",), "USD"),
    ("capital_expenditures", "us-gaap",
     ("PaymentsToAcquirePropertyPlantAndEquipment",), "USD"),
    ("diluted_eps", "us-gaap", ("EarningsPerShareDiluted",), "USD/shares"),
    ("shares_outstanding", "dei", ("EntityCommonStockSharesOutstanding",), "shares"),
)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class _Fact:
    """One usable XBRL fact row, reduced to what the curation needs."""

    value: Decimal
    end: date
    start: date | None
    form: str
    filed: date | None

    @property
    def duration_days(self) -> int:
        return (self.end - self.start).days if self.start is not None else 0

    def beats(self, other: _Fact) -> bool:
        """Selection among rows sharing an end date: a 10-K row beats a 10-Q
        row (the filed annual figure vs a later comparative context); then the
        longest duration within 10-K rows (the full-year value) but the
        SHORTEST within 10-Q rows (the discrete quarter, never the YTD
        roll-up); then the earliest filed date (the original filing, not a
        later re-report — the point-in-time honest first publication)."""
        if (self.form == "10-K") != (other.form == "10-K"):
            return self.form == "10-K"
        if self.duration_days != other.duration_days:
            if self.form == "10-K":
                return self.duration_days > other.duration_days
            return self.duration_days < other.duration_days
        if self.filed is not None and other.filed is not None and self.filed != other.filed:
            return self.filed < other.filed
        if (self.filed is None) != (other.filed is None):
            return self.filed is not None  # a dated row beats an undated one
        return False


def _column(values: Any, index: int) -> str:
    """Safe access into one of the submissions parallel arrays."""
    if isinstance(values, list) and index < len(values) and values[index]:
        return str(values[index])
    return ""


class SecEdgarProvider(MarketDataProvider):
    name = "sec_edgar"

    def __init__(self, *, api_key: str, timeout: float = 10.0,
                 options: dict[str, Any] | None = None) -> None:
        super().__init__(api_key=api_key, timeout=timeout, options=options)
        configured = str(self._options.get("user_agent") or "").strip()
        self._user_agent = configured or (
            f"poseidon/{__version__} "
            f"(set data.providers[].options.user_agent to your contact)")
        self._pace_lock = asyncio.Lock()
        self._last_request = 0.0
        self._cik_by_ticker: dict[str, int] = {}
        self._ciks_fetched_at = 0.0

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.FUNDAMENTALS, DataCapability.FILINGS})

    async def _get(self, url: str) -> Any:
        # Polite pacing: the lock + monotonic gap keeps even concurrent callers
        # from bursting past the SEC fair-access ceiling.
        async with self._pace_lock:
            wait = _MIN_REQUEST_GAP - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        return await self._get_json(url, headers={"User-Agent": self._user_agent})

    async def _cik(self, symbol: str) -> int:
        sym = symbol.strip().upper()
        now = time.monotonic()
        if not self._cik_by_ticker or now - self._ciks_fetched_at > _TICKER_CACHE_TTL:
            payload = await self._get(_TICKERS_URL)
            mapping: dict[str, int] = {}
            rows = payload.values() if isinstance(payload, dict) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker") or "").upper()
                try:
                    cik_number = int(row["cik_str"])
                except (KeyError, TypeError, ValueError):
                    continue
                if ticker:
                    mapping.setdefault(ticker, cik_number)
            if mapping:
                self._cik_by_ticker = mapping
                self._ciks_fetched_at = now
        cik = self._cik_by_ticker.get(sym)
        if cik is None:
            raise ProviderError(self.name, f"unknown SEC ticker {symbol!r}", retryable=False)
        return cik

    # -- fundamentals ------------------------------------------------------------

    async def fundamentals(self, symbol: str) -> FundamentalsReport:
        cik = await self._cik(symbol)
        payload = await self._get(_FACTS_URL.format(cik=cik))
        facts = payload.get("facts") if isinstance(payload, dict) else None
        if not isinstance(facts, dict) or not facts:
            raise ProviderError(self.name, f"no XBRL company facts for {symbol}",
                                retryable=False)
        statements = self._curate(facts)
        if not statements:
            raise ProviderError(
                self.name,
                f"no curated fundamentals for {symbol} (no covered XBRL tags)",
                retryable=False,
            )
        entity_name = payload.get("entityName")
        # Entity name only — EDGAR files no ratios and none are fabricated.
        overview = FundamentalsOverview(name=str(entity_name)) if entity_name else None
        return FundamentalsReport(symbol=symbol, overview=overview, statements=statements,
                                  as_of=self._now(), source=self.name)

    def _curate(self, facts: dict[str, Any]) -> list[StatementPeriod]:
        """Reduce the (potentially multi-MB) companyfacts payload to curated
        StatementPeriods immediately — raw facts never leave this method."""
        per_end: dict[date, dict[str, _Fact]] = {}
        for item, taxonomy, tags, unit in _CONCEPTS:
            best: dict[date, _Fact] = {}
            for fact in self._concept_rows(facts, taxonomy, tags, unit):
                current = best.get(fact.end)
                if current is None or fact.beats(current):
                    best[fact.end] = fact
            for end, fact in best.items():
                per_end.setdefault(end, {})[item] = fact
        periods: list[StatementPeriod] = []
        for end in sorted(per_end, reverse=True)[:_MAX_PERIODS]:
            group = per_end[end]
            annual = any(f.form == "10-K" for f in group.values())
            filed_dates = [f.filed for f in group.values() if f.filed is not None]
            periods.append(StatementPeriod(
                period="annual" if annual else "quarterly",
                fiscal_date_ending=end,
                # Conservative knowable date: the period's figures were all
                # public once the LATEST contributing filing landed (they
                # virtually always share one filing).
                filed=max(filed_dates) if filed_dates else None,
                form="10-K" if annual else "10-Q",
                currency="USD",
                items={name: fact.value for name, fact in group.items()},
            ))
        return periods

    def _concept_rows(self, facts: dict[str, Any], taxonomy: str,
                      tags: tuple[str, ...], unit: str) -> list[_Fact]:
        section = facts.get(taxonomy)
        if not isinstance(section, dict):
            return []
        for tag in tags:  # first tag with usable rows wins (fallback order)
            concept = section.get(tag)
            units = concept.get("units") if isinstance(concept, dict) else None
            rows = units.get(unit) if isinstance(units, dict) else None
            if not isinstance(rows, list):
                continue
            out: list[_Fact] = []
            for row in rows:
                if not isinstance(row, dict) or row.get("form") not in _ACCEPTED_FORMS:
                    continue
                end = _parse_date(row.get("end"))
                if end is None or row.get("val") is None:
                    continue
                try:
                    value = Decimal(str(row["val"]))
                except (InvalidOperation, ValueError):
                    continue
                out.append(_Fact(value=value, end=end, start=_parse_date(row.get("start")),
                                 form=str(row["form"]), filed=_parse_date(row.get("filed"))))
            if out:
                return out
        return []

    # -- filings -----------------------------------------------------------------

    async def filings(self, symbol: str, *, limit: int = 10) -> list[Filing]:
        cik = await self._cik(symbol)
        payload = await self._get(_SUBMISSIONS_URL.format(cik=cik))
        filings_block = payload.get("filings") if isinstance(payload, dict) else None
        recent = filings_block.get("recent") if isinstance(filings_block, dict) else None
        if not isinstance(recent, dict):
            raise ProviderError(self.name, f"no filings for {symbol}", retryable=False)
        forms_raw = recent.get("form")
        forms: list[Any] = forms_raw if isinstance(forms_raw, list) else []
        now = self._now()
        filings: list[Filing] = []
        for i in range(len(forms)):  # parallel arrays are index-aligned, newest first
            if len(filings) >= max(0, limit):
                break
            form = _column(forms, i)
            accession = _column(recent.get("accessionNumber"), i)
            filed = _parse_date(_column(recent.get("filingDate"), i))
            if not form or not accession or filed is None:
                continue
            document = _column(recent.get("primaryDocument"), i)
            url = _ARCHIVES_URL.format(cik=cik, accession=accession.replace("-", ""),
                                       document=document) if document else None
            raw_items = _column(recent.get("items"), i)
            items = [part.strip() for part in raw_items.split(",") if part.strip()]
            filings.append(Filing(
                symbol=symbol, form=form, filed=filed, accession=accession,
                description=_column(recent.get("primaryDocDescription"), i) or None,
                items=items,
                period_end=_parse_date(_column(recent.get("reportDate"), i)),
                url=url, as_of=now, source=self.name,
            ))
        return filings
