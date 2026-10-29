"""One shared pace for every Yahoo Finance request made from this computer.

Yahoo limits how many requests one IP address may make. All fetching (prices and
fundamentals, every thread) goes through GATE, which
  * spaces requests out (YAHOO_RPS, default 1 per second), and
  * when Yahoo answers "Too Many Requests", pauses everything: 1 minute, then
    2, 4, 8 and at most 15 minutes if it keeps happening; successful requests
    shrink the pause again.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

RPS = float(os.environ.get("YAHOO_RPS", "1"))
MAX_PAUSE = 15 * 60

# yfinance logs every failed ticker; the gate prints one line per pause instead.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def is_rate_limit(err: object) -> bool:
    text = f"{type(err).__name__} {err}".lower()
    return "ratelimit" in text or "rate limit" in text or "too many requests" in text or "429" in text


class Gate:
    def __init__(self, rps: float = RPS) -> None:
        self.interval = 1.0 / max(rps, 0.01)
        self.lock = threading.Lock()
        self.next = 0.0      # earliest time for the next request
        self.until = 0.0     # paused until this time
        self.pause = 0       # length of the current/last pause, seconds

    def wait(self, priority: bool = False) -> None:
        """Block until this thread may make one request. Priority requests (someone is
        waiting on the page) skip the queue but still respect a rate-limit pause."""
        while True:
            with self.lock:
                now = time.monotonic()
                if now < self.until:
                    delay = self.until - now
                elif priority:
                    return
                elif now < self.next:
                    delay = self.next - now
                else:
                    self.next = now + self.interval
                    return
            time.sleep(min(delay, 5))

    def limited(self) -> None:
        """Yahoo said "Too Many Requests": pause every request."""
        with self.lock:
            now = time.monotonic()
            if now < self.until:
                return   # another thread already started this pause
            self.pause = min(max(self.pause * 2, 60), MAX_PAUSE)
            self.until = now + self.pause
            pause = self.pause
        print(f"Yahoo rate limit reached: pausing all requests for {pause // 60} min.", file=sys.stderr, flush=True)

    def ok(self) -> None:
        with self.lock:
            if self.pause and time.monotonic() >= self.until:
                self.pause = self.pause // 2 if self.pause > 60 else 0

    def paused_for(self) -> float:
        with self.lock:
            return max(0.0, self.until - time.monotonic())


GATE = Gate()


# ---------- financial statements ----------
# Yahoo's line-item names for each statements.KEYS entry (first name found wins).
# Signs as Yahoo reports them: cash going out (capex, buybacks, dividends) is negative.
STATEMENT_ITEMS = {
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
        "pretaxIncome": ["Pretax Income"],
        "taxProvision": ["Tax Provision"],
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
        "investedCapital": ["Invested Capital"],
    },
    "cashflow": {
        "operatingCF": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
        "capex": ["Capital Expenditure"],
        "fcf": ["Free Cash Flow"],
        "sbc": ["Stock Based Compensation"],
        "da": ["Depreciation And Amortization", "Depreciation Amortization Depletion"],
        "buybacks": ["Repurchase Of Capital Stock", "Common Stock Payments"],
        "dividends": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
    },
}


class RateLimited(Exception):
    """Yahoo is asking us to slow down (or we are already pausing)."""


def _clean(v):
    import math
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


def fetch_statements(ticker: str) -> dict:
    """Annual and quarterly income statement, balance sheet and cash flow (6 requests)."""
    import yfinance as yf
    from ..common import iso, utc_now
    from ..universe import yahoo_symbol
    t = yf.Ticker(yahoo_symbol(ticker))
    raw = {"ticker": ticker, "fetchedAt": iso(utc_now()), "source": "yahoo", "annual": {}, "quarterly": {}}
    for kind, attr in (("income", "income_stmt"), ("balance", "balance_sheet"), ("cashflow", "cashflow")):
        raw["annual"][kind] = _frame_to_periods(_get(t, attr), STATEMENT_ITEMS[kind])
        raw["quarterly"][kind] = _frame_to_periods(_get(t, "quarterly_" + attr), STATEMENT_ITEMS[kind])
    if not any(raw["annual"][k] or raw["quarterly"][k] for k in STATEMENT_ITEMS):
        raise LookupError(f"Yahoo Finance has no financial statements for {ticker}.")
    return raw
