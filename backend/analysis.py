"""Metrics for the Advanced Analysis tab, from one company's financial statements.

    GET /api/analysis/<ticker>[?refresh=1]

The statements come from backend/statements.py (SEC filings first, Yahoo Finance
filling the gaps); this module only computes: growth, margins, ROIC, free cash
flow and the rest, grouped into sections (profitability, balance sheet, cash flow).

Annual view: the last 5 fiscal years plus a TTM column (sum of the last four
quarters; the latest quarter for balance-sheet items). Quarterly view: the last 8
quarters. Growth is year over year: previous fiscal year, or the same quarter a
year earlier, matched by date. Fiscal years are named after the calendar year they
end in.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import statements
from .sources.yahoo import RateLimited  # noqa: F401  (the server reports it as "Yahoo is limiting requests")
from .statements import FLOWS, KEYS

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


def get(ticker: str, refresh: bool = False) -> dict:
    """Analysis for one ticker. Raises ValueError for a malformed ticker, LookupError when no
    source has statements, or the source's error (e.g. RateLimited) when nothing is cached."""
    ticker = ticker.strip().upper().replace("-", ".")
    if not TICKER_RE.match(ticker):
        raise ValueError(f"{ticker!r} doesn't look like a ticker.")
    raw = statements.get(ticker, refresh=refresh)
    out = compute(raw)
    out["sources"] = raw.get("sources", [])
    if raw.get("warnings"):
        out["warning"] = " · ".join(raw["warnings"])
    return out


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
    for kind in KEYS:
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
    if p.get("ebitda") is None and p.get("operatingIncome") is not None and p.get("da") is not None:
        p["ebitda"] = p["operatingIncome"] + p["da"]
    if p.get("nonCurrentAssets") is None and p.get("totalAssets") is not None and p.get("currentAssets") is not None:
        p["nonCurrentAssets"] = p["totalAssets"] - p["currentAssets"]
    if p.get("nonCurrentLiabilities") is None and p.get("totalLiabilities") is not None and p.get("currentLiabilities") is not None:
        p["nonCurrentLiabilities"] = p["totalLiabilities"] - p["currentLiabilities"]
    return p


def _ttm(quarters: list[tuple[str, dict]]) -> dict | None:
    """Last four quarters summed for flow items, latest quarter for balance items."""
    if len(quarters) < 4:
        return None
    first, last = _date(quarters[-4][0]), _date(quarters[-1][0])
    if not first or not last or not 250 <= (last - first).days <= 300:
        return None   # not four consecutive quarters (one is missing): no TTM
    last4 = [q for _, q in quarters[-4:]]
    out = {}
    flow_keys = {k for kind in FLOWS for k in KEYS[kind]}
    for k in flow_keys:
        if k == "shares":
            continue
        vals = [q.get(k) for q in last4]
        if all(v is not None for v in vals):
            out[k] = sum(vals)
    for k in KEYS["balance"]:
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
    ("profit", "roic", "ROIC", "pct", False,
     "Return on invested capital: operating income after tax ÷ (debt + shareholders' equity). Quarters annualized."),
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
    ("balance", "netDebtToEbitda", "Net debt / EBITDA", "ratio", False, "(Total debt − cash) ÷ EBITDA: years of earnings to repay net debt; negative = net cash"),
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
    # ROIC = NOPAT / invested capital. Tax rate from the period's tax provision ÷ pretax income
    # (kept between 0 and 50%), 21% if not reported. Invested capital: Yahoo's figure (debt +
    # equity), else total debt + equity.
    oi = p.get("operatingIncome")
    pre, tax = p.get("pretaxIncome"), p.get("taxProvision")
    rate = tax / pre if pre and pre > 0 and tax is not None else 0.21
    rate = min(max(rate, 0.0), 0.5)
    ic = p.get("investedCapital")
    if ic is None and p.get("equity") is not None:
        ic = p["equity"] + (p.get("totalDebt") or 0)
    p["roic"] = _pct(oi * (1 - rate) * annualize, ic) if oi is not None and ic and ic > 0 else None
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
    # years of EBITDA to pay off net debt; negative = more cash than debt. Quarters annualized.
    ebitda = p.get("ebitda")
    p["netDebtToEbitda"] = _div(p.get("netDebt"), ebitda * annualize) if p.get("netDebt") is not None and ebitda and ebitda > 0 else None
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
    # the period about a year before each one, matched by date so a quarter missing
    # from Yahoo's data can't make growth compare against the wrong period
    ends = [_date(c["end"]) for c in cols]
    for i, c in enumerate(cols):
        if c.get("ttm") or ends[i] is None:
            continue
        best = None
        for j in range(i):
            if ends[j] is None or cols[j].get("ttm"):
                continue
            days = (ends[i] - ends[j]).days
            if 330 <= days <= 400 and (best is None or abs(days - 365) < abs((ends[i] - ends[best]).days - 365)):
                best = j
        if best is not None:
            c["prev"] = best
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
                        j = cols[i].get("prev")
                        g.append(_round(_growth(vals[i].get(key), vals[j].get(key)), "pct") if j is not None else None)
                row["growth"] = g
            rows.append(row)
        sections.append({"id": sid, "title": title, "rows": rows})
    return {"periods": cols, "sections": sections}


def _round(v, kind):
    if v is None:
        return None
    return round(v, 4 if kind in ("ratio", "eps") else 2 if kind == "pct" else 0)


def _date(end: str | None):
    try:
        return datetime.strptime(end, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _label(end: str, quarterly: bool) -> str:
    try:
        d = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return end
    if quarterly:
        return f"{d.strftime('%b')} {d.year}"
    # named after the year it ends in, as companies do (NVIDIA's FY2026 ends January 2026)
    return f"FY{d.year}"


def compute(raw: dict) -> dict:
    annual = sorted(_merge(raw.get("annual", {})).items())
    quarterly = sorted(_merge(raw.get("quarterly", {})).items())
    annual = [(e, _derive(p)) for e, p in annual][-5:]
    quarterly = [(e, _derive(p)) for e, p in quarterly][-8:]
    ttm = _ttm(quarterly)
    if ttm and annual and quarterly and quarterly[-1][0] <= annual[-1][0]:
        ttm = None   # the latest quarter is already the fiscal year end: TTM = last FY
    latest_q = statements.latest_quarter_end(raw)
    return {
        "ticker": raw.get("ticker"),
        "fetchedAt": raw.get("fetchedAt"),
        "latestQuarter": latest_q.isoformat() if latest_q else None,
        "quarterOverdue": statements.quarter_overdue(raw),
        "currency": raw.get("currency", "USD"),
        "annual": _view(annual, False, ttm),
        "quarterly": _view(quarterly, True, None),
    }
