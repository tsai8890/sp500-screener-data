"""Per-stock financial statements for the Advanced Analysis tab.

    GET /api/analysis/<ticker>[?refresh=1]

Fetched from Yahoo Finance on demand (6 requests: income statement, balance sheet
and cash flow, annual and quarterly), cached in data/analysis/<TICKER>.json for a
day (statements only change once a quarter), and turned into three sections of
metrics: profitability, balance sheet, cash flow.

Annual view: the last 4-5 fiscal years plus a TTM column (sum of the last four
quarters; the latest quarter for balance-sheet items). Quarterly view: the
quarters Yahoo provides (usually 5-6). Growth is year over year: previous fiscal
year, or the same quarter a year earlier.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from datetime import datetime

import yfinance as yf

from .common import ROOT, iso, parse_iso, read_json, utc_now, write_json
from .universe import yahoo_symbol
from .yahoo import GATE, is_rate_limit

CACHE_DIR = os.environ.get("SCREENER_ANALYSIS_DIR", os.path.join(ROOT, "data", "analysis"))
MAX_AGE = 24 * 3600        # cached statements are fetched again after a day
MIN_REFRESH = 10 * 60      # "Refresh" does nothing if the data is younger than this
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

# Yahoo line items to keep (first name found wins).
ITEMS = {
    "income": {
        "revenue": ["Total Revenue", "Operating Revenue"],
        "costOfRevenue": ["Cost Of Revenue", "Reconciled Cost Of Revenue"],
        "grossProfit": ["Gross Profit"],
        "rnd": ["Research And Development"],
        "operatingIncome": ["Operating Income", "Total Operating Income As Reported"],
        "ebitda": ["EBITDA", "Normalized EBITDA"],
        "netIncome": ["Net Income Common Stockholders", "Net Income", "Net Income Including Noncontrolling Interests"],
        "eps": ["Diluted EPS"],
        "shares": ["Diluted Average Shares"],
    },
    "balance": {
        "cash": ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"],
        "receivables": ["Accounts Receivable", "Receivables"],
        "inventory": ["Inventory"],
        "currentAssets": ["Current Assets"],
        "nonCurrentAssets": ["Total Non Current Assets"],
        "totalAssets": ["Total Assets"],
        "currentLiabilities": ["Current Liabilities"],
        "nonCurrentLiabilities": ["Total Non Current Liabilities Net Minority Interest"],
        "totalLiabilities": ["Total Liabilities Net Minority Interest"],
        "totalDebt": ["Total Debt"],
        "equity": ["Stockholders Equity", "Common Stock Equity"],
    },
    "cashflow": {
        "operatingCF": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
        "capex": ["Capital Expenditure"],
        "fcf": ["Free Cash Flow"],
        "sbc": ["Stock Based Compensation"],
        "buybacks": ["Repurchase Of Capital Stock", "Common Stock Payments"],
        "dividends": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
    },
}
FLOWS = ("income", "cashflow")   # summed for TTM; balance-sheet items are point-in-time

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class RateLimited(Exception):
    pass


# ---------- fetching ----------

def _clean(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _frame_to_periods(df, items: dict) -> dict:
    """yfinance statement DataFrame (rows: line items, columns: period end dates)
    -> {"YYYY-MM-DD": {key: value}}"""
    out: dict[str, dict] = {}
    if df is None or getattr(df, "empty", True):
        return out
    index = set(df.index)
    for col in df.columns:
        end = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
        rec = {}
        for key, names in items.items():
            for name in names:
                if name in index:
                    v = _clean(df.at[name, col])
                    if v is not None:
                        rec[key] = v
                        break
        if rec:
            out[end] = rec
    return out


def _get(t, attr: str):
    if GATE.paused_for():
        raise RateLimited("paused")   # answer the page now instead of making it wait out the pause
    GATE.wait(priority=True)
    try:
        df = getattr(t, attr)
        GATE.ok()
        return df
    except Exception as e:  # noqa: BLE001
        if is_rate_limit(e):
            GATE.limited()
            raise RateLimited(str(e)) from e
        raise


def fetch_raw(ticker: str) -> dict:
    t = yf.Ticker(yahoo_symbol(ticker))
    raw = {"ticker": ticker, "fetchedAt": iso(utc_now()), "annual": {}, "quarterly": {}}
    for kind, attr in (("income", "income_stmt"), ("balance", "balance_sheet"), ("cashflow", "cashflow")):
        raw["annual"][kind] = _frame_to_periods(_get(t, attr), ITEMS[kind])
        raw["quarterly"][kind] = _frame_to_periods(_get(t, "quarterly_" + attr), ITEMS[kind])
    if not any(raw["annual"][k] or raw["quarterly"][k] for k in ITEMS):
        raise LookupError(f"Yahoo Finance has no financial statements for {ticker}.")
    return raw


def _cache_path(ticker: str) -> str:
    return os.path.join(CACHE_DIR, ticker.replace("/", "_") + ".json")


def _age(raw: dict | None) -> float:
    dt = parse_iso((raw or {}).get("fetchedAt"))
    return (utc_now() - dt).total_seconds() if dt else float("inf")


def get(ticker: str, refresh: bool = False) -> dict:
    """Analysis for one ticker: from the cache when fresh enough, otherwise from Yahoo.
    If Yahoo fails but an older copy exists, that copy is returned with "warning"."""
    ticker = ticker.strip().upper().replace("-", ".")
    if not TICKER_RE.match(ticker):
        raise ValueError(f"{ticker!r} doesn't look like a ticker.")
    with _locks_guard:
        lock = _locks.setdefault(ticker, threading.Lock())
    with lock:   # two tabs asking for the same stock fetch it once
        cached = read_json(_cache_path(ticker))
        age = _age(cached)
        if cached and (age < MIN_REFRESH or (age < MAX_AGE and not refresh)):
            return compute(cached)
        try:
            raw = fetch_raw(ticker)
        except (RateLimited, Exception) as e:  # noqa: BLE001
            if cached and not isinstance(e, (LookupError, ValueError)):
                out = compute(cached)
                out["warning"] = ("Yahoo is limiting requests right now, so this is the copy from "
                                  if isinstance(e, RateLimited) else "Couldn't reach Yahoo Finance, so this is the copy from ") + cached["fetchedAt"]
                return out
            raise
        os.makedirs(CACHE_DIR, exist_ok=True)
        write_json(_cache_path(ticker), raw)
        return compute(raw)


# ---------- metrics ----------

def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _pct(a, b):
    v = _div(a, b)
    return None if v is None else v * 100


def _growth(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100


def _merge(view: dict) -> dict[str, dict]:
    """{end date: {all keys}} across the three statements of one view."""
    out: dict[str, dict] = {}
    for kind in ITEMS:
        for end, rec in (view.get(kind) or {}).items():
            out.setdefault(end, {}).update(rec)
    return out


def _derive(p: dict) -> dict:
    """Fill in values Yahoo sometimes leaves out."""
    p = dict(p)
    if p.get("grossProfit") is None and p.get("revenue") is not None and p.get("costOfRevenue") is not None:
        p["grossProfit"] = p["revenue"] - p["costOfRevenue"]
    if p.get("fcf") is None and p.get("operatingCF") is not None and p.get("capex") is not None:
        p["fcf"] = p["operatingCF"] + p["capex"]          # capex is negative in Yahoo's data
    if p.get("nonCurrentAssets") is None and p.get("totalAssets") is not None and p.get("currentAssets") is not None:
        p["nonCurrentAssets"] = p["totalAssets"] - p["currentAssets"]
    if p.get("nonCurrentLiabilities") is None and p.get("totalLiabilities") is not None and p.get("currentLiabilities") is not None:
        p["nonCurrentLiabilities"] = p["totalLiabilities"] - p["currentLiabilities"]
    return p


def _ttm(quarters: list[tuple[str, dict]]) -> dict | None:
    """Last four quarters summed for flow items, latest quarter for balance items."""
    if len(quarters) < 4:
        return None
    last4 = [q for _, q in quarters[-4:]]
    out = {}
    flow_keys = {k for kind in FLOWS for k in ITEMS[kind]} | {"fcf"}
    for k in flow_keys:
        if k == "shares":
            continue
        vals = [q.get(k) for q in last4]
        if all(v is not None for v in vals):
            out[k] = sum(vals)
    for k in ITEMS["balance"]:
        if last4[-1].get(k) is not None:
            out[k] = last4[-1][k]
    if last4[-1].get("shares") is not None:
        out["shares"] = last4[-1]["shares"]
    return _derive(out)


ROWS = [
    # section, key, label, kind, show growth, hint
    ("profit", "revenue", "Total revenue", "money", True, None),
    ("profit", "grossProfit", "Gross profit", "money", True, None),
    ("profit", "operatingIncome", "Operating income", "money", True, None),
    ("profit", "netIncome", "Net income", "money", True, None),
    ("profit", "eps", "Diluted EPS", "eps", True, None),
    ("profit", "grossMargin", "Gross margin", "pct", False, "Gross profit ÷ revenue"),
    ("profit", "operatingMargin", "Operating margin", "pct", False, "Operating income ÷ revenue"),
    ("profit", "netMargin", "Net margin", "pct", False, "Net income ÷ revenue"),
    ("profit", "ebitdaMargin", "EBITDA margin", "pct", False, "EBITDA ÷ revenue"),
    ("profit", "rndRatio", "R&D / revenue", "pct", False, None),
    ("profit", "roe", "Return on equity", "pct", False, "Net income ÷ shareholders' equity (quarters annualized)"),
    ("profit", "roa", "Return on assets", "pct", False, "Net income ÷ total assets (quarters annualized)"),
    ("balance", "cash", "Cash & short-term investments", "money", False, None),
    ("balance", "receivables", "Accounts receivable", "money", False, None),
    ("balance", "inventory", "Inventory", "money", False, None),
    ("balance", "currentAssets", "Current assets", "money", False, "Short-term: cash or turned into cash within a year"),
    ("balance", "nonCurrentAssets", "Non-current assets", "money", False, "Long-term: property, equipment, goodwill, long-term investments"),
    ("balance", "totalAssets", "Total assets", "money", True, None),
    ("balance", "currentLiabilities", "Current liabilities", "money", False, "Due within a year"),
    ("balance", "nonCurrentLiabilities", "Non-current liabilities", "money", False, "Due after a year, mostly long-term debt"),
    ("balance", "totalLiabilities", "Total liabilities", "money", True, None),
    ("balance", "totalDebt", "Total debt", "money", False, None),
    ("balance", "netDebt", "Net debt", "money", False, "Total debt − cash & short-term investments; negative = net cash"),
    ("balance", "equity", "Shareholders' equity", "money", True, None),
    ("balance", "currentRatio", "Current ratio", "ratio", False, "Current assets ÷ current liabilities; above 1 covers next year's bills"),
    ("balance", "quickRatio", "Quick ratio", "ratio", False, "(Current assets − inventory) ÷ current liabilities"),
    ("balance", "debtToEquity", "Debt / equity", "ratio", False, "Total debt ÷ shareholders' equity"),
    ("balance", "liabToAssets", "Liabilities / assets", "pct", False, None),
    ("cash", "operatingCF", "Operating cash flow", "money", True, None),
    ("cash", "capex", "Capital expenditure", "money", False, "Cash spent on property and equipment"),
    ("cash", "fcf", "Free cash flow", "money", True, "Operating cash flow − capital expenditure"),
    ("cash", "fcfMargin", "FCF margin", "pct", False, "Free cash flow ÷ revenue"),
    ("cash", "fcfConversion", "FCF / net income", "pct", False, "How much of the reported profit arrives as cash"),
    ("cash", "sbc", "Stock-based compensation", "money", False, None),
    ("cash", "fcfAfterSbc", "FCF after SBC", "money", False, "Free cash flow − stock-based compensation"),
    ("cash", "fcfPerShare", "FCF per share", "eps", True, None),
    ("cash", "buybacks", "Share buybacks", "money", False, None),
    ("cash", "dividends", "Dividends paid", "money", False, None),
]
SECTIONS = [("profit", "Profitability"), ("balance", "Balance sheet"), ("cash", "Cash flow")]


def _ratios(p: dict, annualize: float) -> dict:
    p = dict(p)
    rev = p.get("revenue")
    p["grossMargin"] = _pct(p.get("grossProfit"), rev)
    p["operatingMargin"] = _pct(p.get("operatingIncome"), rev)
    p["netMargin"] = _pct(p.get("netIncome"), rev)
    p["ebitdaMargin"] = _pct(p.get("ebitda"), rev)
    p["rndRatio"] = _pct(p.get("rnd"), rev)
    ni = p.get("netIncome")
    eq = p.get("equity")
    p["roe"] = _pct(ni * annualize, eq) if ni is not None and eq and eq > 0 else None
    p["roa"] = _pct(ni * annualize if ni is not None else None, p.get("totalAssets"))
    if p.get("totalDebt") is not None and p.get("cash") is not None:
        p["netDebt"] = p["totalDebt"] - p["cash"]
    p["currentRatio"] = _div(p.get("currentAssets"), p.get("currentLiabilities"))
    if p.get("currentAssets") is not None:
        p["quickRatio"] = _div(p["currentAssets"] - (p.get("inventory") or 0), p.get("currentLiabilities"))
    p["debtToEquity"] = _div(p.get("totalDebt"), eq) if eq and eq > 0 else None
    p["liabToAssets"] = _pct(p.get("totalLiabilities"), p.get("totalAssets"))
    fcf = p.get("fcf")
    p["fcfMargin"] = _pct(fcf, rev)
    p["fcfConversion"] = _pct(fcf, ni) if ni and ni > 0 else None
    if fcf is not None and p.get("sbc") is not None:
        p["fcfAfterSbc"] = fcf - p["sbc"]
    p["fcfPerShare"] = _div(fcf, p.get("shares"))
    return p


def _view(periods: list[tuple[str, dict]], quarterly: bool, ttm: dict | None) -> dict:
    """periods oldest first -> {"periods": [...], "sections": [...]}"""
    annualize = 4.0 if quarterly else 1.0
    cols = [{"end": end, "label": _label(end, quarterly)} for end, _ in periods]
    vals = [_ratios(p, annualize) for _, p in periods]
    if ttm:
        cols.append({"end": periods[-1][0] if periods else None, "label": "TTM", "ttm": True})
        vals.append(_ratios(ttm, 1.0))
    lag = 4 if quarterly else 1
    sections = []
    for sid, title in SECTIONS:
        rows = []
        for sec, key, label, kind, growth, hint in ROWS:
            if sec != sid:
                continue
            values = [_round(v.get(key), kind) for v in vals]
            if all(v is None for v in values):
                continue
            row = {"key": key, "label": label, "kind": kind, "values": values}
            if hint:
                row["hint"] = hint
            if growth:
                g = []
                for i in range(len(vals)):
                    if cols[i].get("ttm"):
                        g.append(None)   # TTM has no matching period a year earlier here
                    else:
                        j = i - lag
                        g.append(_round(_growth(vals[i].get(key), vals[j].get(key)), "pct") if j >= 0 else None)
                row["growth"] = g
            rows.append(row)
        sections.append({"id": sid, "title": title, "rows": rows})
    return {"periods": cols, "sections": sections}


def _round(v, kind):
    if v is None:
        return None
    return round(v, 4 if kind in ("ratio", "eps") else 2 if kind == "pct" else 0)


def _label(end: str, quarterly: bool) -> str:
    try:
        d = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return end
    if quarterly:
        return f"{d.strftime('%b')} {d.year}"
    # fiscal years ending in January belong to the previous year (retailers)
    return f"FY{d.year - 1 if d.month == 1 else d.year}"


def compute(raw: dict) -> dict:
    annual = sorted(_merge(raw.get("annual", {})).items())
    quarterly = sorted(_merge(raw.get("quarterly", {})).items())
    annual = [(e, _derive(p)) for e, p in annual][-5:]
    quarterly = [(e, _derive(p)) for e, p in quarterly][-8:]
    ttm = _ttm(quarterly)
    if ttm and annual and quarterly and quarterly[-1][0] <= annual[-1][0]:
        ttm = None   # the latest quarter is already the fiscal year end: TTM = last FY
    return {
        "ticker": raw.get("ticker"),
        "fetchedAt": raw.get("fetchedAt"),
        "currency": raw.get("currency", "USD"),
        "annual": _view(annual, False, ttm),
        "quarterly": _view(quarterly, True, None),
    }
