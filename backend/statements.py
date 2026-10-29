"""Financial statements for one company, combined from every source.

  SEC EDGAR first: official, and new quarters appear minutes after a 10-Q / 10-K is
  filed, days before Yahoo has them. Yahoo Finance fills what the SEC data lacks
  (a line item a company doesn't tag, periods before the SEC data starts) and covers
  banks and insurers, whose filings don't fit the usual statement layout.

Each source keeps its own cache in data/statements/<TICKER>.<source>.json and its
own refresh schedule, so a failing source never blocks the other.

Shape (shared by every source, see backend/sources/__init__.py):
  {"annual":    {"income": {end: {key: value}}, "balance": {...}, "cashflow": {...}},
   "quarterly": {...}}
Money in the reporting currency; cash going out (capex, buybacks, dividends) negative.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .common import FUNDAMENTALS_PATH, ROOT, iso, parse_iso, read_json, utc_now, write_json
from .sources import sec, sec_xbrl, yahoo

# The keys every source maps into.
KEYS = {
    "income": ["revenue", "costOfRevenue", "grossProfit", "rnd", "operatingIncome", "ebitda", "netIncome",
               "eps", "shares", "pretaxIncome", "taxProvision"],
    "balance": ["cash", "receivables", "inventory", "currentAssets", "nonCurrentAssets", "totalAssets",
                "currentLiabilities", "nonCurrentLiabilities", "totalLiabilities", "totalDebt", "equity",
                "investedCapital"],
    "cashflow": ["operatingCF", "capex", "fcf", "sbc", "da", "buybacks", "dividends"],
}
FLOWS = ("income", "cashflow")   # add up over quarters (TTM); balances are a single date

CACHE_DIR = os.environ.get("SCREENER_STATEMENTS_DIR", os.path.join(ROOT, "data", "statements"))
MIN_REFRESH = 60           # "Refresh" refetches unless the copy is less than a minute old
QUARTER_DUE_DAYS = 95      # newest quarter older than this: the next one is (about to be) out
SKIP_SEC_SECTORS = {"Financials"}   # bank / insurer filings don't map onto these statements


@dataclass
class Provider:
    name: str                        # also the cache file suffix
    label: str                       # shown on the page
    fetch: Callable[[str], dict]     # ticker -> statements (raises on failure)
    enabled: Callable[[], bool]
    max_age: int                     # seconds before a cached copy is fetched again
    max_age_due: int                 # ... while a new quarter is overdue


def _fetch_sec(ticker: str) -> dict:
    cik = sec.cik_for(ticker)
    if cik is None:
        raise LookupError(f"The SEC doesn't list {ticker}.")
    return sec_xbrl.statements(sec.company_facts(cik, priority=True), ticker, iso(utc_now()))


PROVIDERS = [   # in order of preference
    Provider("sec", "SEC filings (10-Q / 10-K)", _fetch_sec, sec.enabled, 6 * 3600, 30 * 60),
    Provider("yahoo", "Yahoo Finance", yahoo.fetch_statements, lambda: True, 24 * 3600, 3 * 3600),
]

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


# ---------- cache ----------

def cache_path(ticker: str, source: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticker.replace('/', '_')}.{source}.json")


def load(ticker: str, source: str) -> dict | None:
    return read_json(cache_path(ticker, source))


def save(ticker: str, source: str, raw: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    write_json(cache_path(ticker, source), raw)


def _age(raw: dict | None) -> float:
    dt = parse_iso((raw or {}).get("fetchedAt"))
    return (utc_now() - dt).total_seconds() if dt else float("inf")


# ---------- dates ----------

def _d(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def latest_quarter_end(raw: dict) -> date | None:
    ends = [d for d in (_d(e) for kind in FLOWS for e in (raw.get("quarterly", {}).get(kind) or {})) if d]
    return max(ends) if ends else None


def quarter_overdue(raw: dict) -> bool:
    end = latest_quarter_end(raw)
    return bool(end) and (datetime.utcnow().date() - end).days > QUARTER_DUE_DAYS


# ---------- merge ----------

def merge(raws: list[dict]) -> dict:
    """Combine sources, earlier ones winning: per period and per value. Periods are matched
    by end date (within a week: fiscal periods can end on slightly different days)."""
    out = {"annual": {k: {} for k in KEYS}, "quarterly": {k: {} for k in KEYS}}
    for view in out:
        ends: list[date] = []          # canonical period ends, first source's spelling
        for raw in raws:
            for kind in KEYS:
                for end, rec in (raw.get(view, {}).get(kind) or {}).items():
                    d = _d(end)
                    if d is None:
                        continue
                    match = next((e for e in ends if abs((e - d).days) <= 7), None)
                    if match is None:
                        ends.append(d)
                        match = d
                    target = out[view][kind].setdefault(match.isoformat(), {})
                    for k, v in rec.items():
                        target.setdefault(k, v)   # an earlier source already has it: keep that
    return out


# ---------- the one entry point ----------

def _sector(ticker: str) -> str | None:
    stock = ((read_json(FUNDAMENTALS_PATH) or {}).get("stocks") or {}).get(ticker) or {}
    return stock.get("sector")


def get(ticker: str, refresh: bool = False) -> dict:
    """Merged statements for one ticker, plus "sources" (what each source contributed) and
    "warnings". Raises the first source's error if no source has anything."""
    with _locks_guard:
        lock = _locks.setdefault(ticker, threading.Lock())
    with lock:   # two pages asking for the same stock fetch it once
        sector = _sector(ticker)
        got, sources, warnings, errors = [], [], [], []
        for p in PROVIDERS:
            if not p.enabled() or (p.name == "sec" and sector in SKIP_SEC_SECTORS):
                continue
            cached = load(ticker, p.name)
            age = _age(cached)
            limit = p.max_age_due if cached and quarter_overdue(cached) else p.max_age
            raw = cached
            if not cached or age >= MIN_REFRESH and (refresh or age >= limit):
                try:
                    raw = p.fetch(ticker)
                    save(ticker, p.name, raw)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    if cached:
                        warnings.append(f"{p.label}: couldn't update ({_why(e)}); showing the copy from {cached.get('fetchedAt')}")
            if raw:
                got.append(raw)
                q = latest_quarter_end(raw)
                sources.append({"source": p.name, "label": p.label, "fetchedAt": raw.get("fetchedAt"),
                                "latestQuarter": q.isoformat() if q else None})
        if not got:
            raise errors[0] if errors else LookupError(f"No financial statements for {ticker}.")
        merged = merge(got)
        merged.update({"ticker": ticker, "fetchedAt": max(s["fetchedAt"] or "" for s in sources),
                       "sources": sources, "warnings": warnings})
        return merged


def _why(e: Exception) -> str:
    if isinstance(e, yahoo.RateLimited):
        return "Yahoo is limiting requests"
    if isinstance(e, sec.Unavailable):
        return str(e)
    if isinstance(e, LookupError):
        return "no data"
    return type(e).__name__
