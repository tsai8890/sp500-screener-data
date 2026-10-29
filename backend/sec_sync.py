"""Keeps every stock's SEC statements current, for the Screener.

On start: downloads the SEC data of every stock that has none yet, or whose copy is
more than REFRESH_DAYS old (about 1,000 companies the first time; it runs in the
background and the page keeps working). Then every FEED_EVERY seconds it reads the
SEC's list of newest 10-Q / 10-K filings and fetches those companies again, so a
company's new quarter is in the Screener minutes after it files.

From each company's statements it works out the Screener figures that come from
filings (overriding Yahoo's, which lag for days after a report):
  eps        trailing 12-month diluted EPS (sum of the last four quarters)
  revGrowth  latest quarter's revenue vs the same quarter a year earlier, %
  netMargin  net income / revenue over the last four quarters, %
saved in data/sec_fundamentals.json and applied by build.py.
Banks and insurers (Financials) keep Yahoo's figures.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date

from . import statements
from .common import SEC_FUNDAMENTALS_PATH as SEC_FUND_PATH, iso, parse_iso, read_json, utc_now, write_json
from .sources import sec, sec_xbrl

REFRESH_DAYS = 7         # re-download a company's data at least this often
FEED_EVERY = 10 * 60     # check the SEC's newest filings this often
RETRY_NEW = 15 * 60      # a just-filed report may need a few minutes to reach the data API
SAVE_EVERY = 25          # write the Screener file after this many companies
FORMS = ("10-Q", "10-K")


# ---------- Screener figures from statements ----------

def _d(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def screener_figures(raw: dict) -> dict | None:
    """eps / revGrowth / netMargin from a company's quarterly statements, or None."""
    q = raw.get("quarterly", {})
    inc = q.get("income") or {}
    ends = sorted(e for e in inc if _d(e))
    if len(ends) < 4:
        return None
    last4 = ends[-4:]
    if not 250 <= (_d(last4[-1]) - _d(last4[0])).days <= 300:
        return None   # a quarter is missing
    out = {"latestQuarter": last4[-1]}
    eps = [inc[e].get("eps") for e in last4]
    if all(v is not None for v in eps):
        out["eps"] = round(sum(eps), 4)
    rev = [inc[e].get("revenue") for e in last4]
    ni = [inc[e].get("netIncome") for e in last4]
    if all(v is not None for v in rev) and all(v is not None for v in ni) and sum(rev) > 0:
        out["netMargin"] = round(sum(ni) / sum(rev) * 100, 1)
    latest = inc[last4[-1]].get("revenue")
    year_ago = next((inc[e].get("revenue") for e in ends
                     if 350 <= (_d(last4[-1]) - _d(e)).days <= 380), None)
    if latest is not None and year_ago:
        out["revGrowth"] = round((latest - year_ago) / abs(year_ago) * 100, 1)
    return out if len(out) > 1 else None


def overrides() -> dict:
    """{ticker: {eps, revGrowth, netMargin}} for build.py."""
    return (read_json(SEC_FUND_PATH) or {}).get("stocks", {})


# ---------- the job ----------

class Sync:
    def __init__(self, stocks, on_update) -> None:
        self.stocks = stocks             # () -> [(ticker, sector)]
        self.on_update = on_update       # called after the Screener file changes
        data = read_json(SEC_FUND_PATH) or {}
        self.figures: dict = data.get("stocks", {})
        self.priority: dict[str, float] = {}     # ticker -> earliest time to fetch
        self.wanted: dict[str, str] = {}         # ticker -> filing date we are waiting for
        self.dirty = 0
        self.last_feed = 0.0

    # which stocks this job covers, and their CIKs
    def covered(self) -> dict[str, int]:
        out = {}
        for t, sector in self.stocks():
            if sector in statements.SKIP_SEC_SECTORS:
                continue
            cik = sec.cik_for(t)
            if cik:
                out[t] = cik
        return out

    def stale(self, covered: dict[str, int]) -> list[str]:
        def age(t):
            raw = statements.load(t, "sec")
            dt = parse_iso((raw or {}).get("fetchedAt"))
            return (utc_now() - dt).total_seconds() if dt else float("inf")
        ages = {t: age(t) for t in covered}
        return sorted((t for t, a in ages.items() if a > REFRESH_DAYS * 86400), key=lambda t: -ages[t])

    def check_feed(self, covered: dict[str, int]) -> None:
        by_cik: dict[int, list[str]] = {}
        for t, cik in covered.items():
            by_cik.setdefault(cik, []).append(t)
        for form in FORMS:
            for f in sec.latest_filings(form):
                tickers = by_cik.get(f["cik"])
                if not tickers:
                    continue
                filed = (f["updated"] or "")[:10]
                have = (statements.load(tickers[0], "sec") or {}).get("lastFiled") or ""
                if filed and filed > have and tickers[0] not in self.priority:
                    print(f"SEC: new {f['form']} from {f['company']} ({', '.join(tickers)}); fetching it.", flush=True)
                    for t in tickers:
                        self.priority[t] = time.time()
                        self.wanted[t] = filed

    def fetch(self, ticker: str, cik: int, same_cik: list[str]) -> None:
        raw = sec_xbrl.statements(sec.company_facts(cik), ticker, iso(utc_now()))
        for t in same_cik:                 # GOOG and GOOGL are one company
            statements.save(t, "sec", {**raw, "ticker": t})
            fig = screener_figures(raw)
            if fig:
                self.figures[t] = fig
            else:
                self.figures.pop(t, None)
            want = self.wanted.get(t)
            if want and (raw.get("lastFiled") or "") < want:
                self.priority[t] = time.time() + RETRY_NEW   # not in the data API yet: try again soon
            else:
                self.priority.pop(t, None)
                self.wanted.pop(t, None)
        self.dirty += 1

    def save(self) -> None:
        os.makedirs(os.path.dirname(SEC_FUND_PATH), exist_ok=True)
        write_json(SEC_FUND_PATH, {"asOf": iso(utc_now()), "stocks": self.figures})
        self.dirty = 0
        self.on_update()

    def run(self) -> None:
        if not sec.enabled():
            print("SEC: no SEC_USER_AGENT (your name and email, in .env); financial statements come from "
                  "Yahoo only.", flush=True)
            return
        try:
            covered = self.covered()
        except sec.Unavailable as e:
            print(f"SEC: {e}; trying again in 10 min.", file=sys.stderr, flush=True)
            time.sleep(600)
            return self.run()
        queue = self.stale(covered)
        if queue:
            mins = max(1, round(len(queue) / sec.RPS / 60 * 2))
            print(f"SEC: downloading filings data for {len(queue)} companies (about {mins} min, in the "
                  f"background)...", flush=True)
        done, total = 0, len(queue)
        while True:
            try:
                if time.time() - self.last_feed > FEED_EVERY:
                    self.last_feed = time.time()
                    covered = self.covered()
                    self.check_feed(covered)
                now = time.time()
                due = [t for t, at in self.priority.items() if at <= now]
                ticker = due[0] if due else (queue.pop(0) if queue else None)
                if ticker is None:
                    if self.dirty:
                        self.save()
                    if total and done >= total:
                        print(f"SEC: all {total} companies up to date.", flush=True)
                        total = 0
                    time.sleep(30)
                    if not queue and not self.priority:
                        queue = self.stale(covered)   # the weekly re-download, a few at a time
                    continue
                cik = covered.get(ticker)
                if cik:
                    same = [t for t, c in covered.items() if c == cik]
                    try:
                        self.fetch(ticker, cik, same)
                    except LookupError:
                        self.priority.pop(ticker, None)   # the SEC has no XBRL data for it
                    except sec.Unavailable:
                        raise
                    except Exception as e:  # noqa: BLE001  (one bad company must not stop the rest)
                        print(f"SEC: {ticker}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                        if ticker in self.priority:
                            self.priority[ticker] = time.time() + RETRY_NEW
                    for t in same:
                        if t != ticker and t in queue:
                            queue.remove(t)
                    if due:
                        self.save()                       # a new report: show it right away
                    else:
                        done += 1
                        if total and done % 100 == 0:
                            print(f"SEC: {done}/{total} ...", flush=True)
                if self.dirty >= SAVE_EVERY:
                    self.save()
            except sec.Unavailable as e:
                print(f"SEC: {e}; pausing 10 min.", file=sys.stderr, flush=True)
                time.sleep(600)
            except Exception as e:  # noqa: BLE001
                print(f"SEC: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                time.sleep(5)


def loop(stocks, on_update) -> None:
    Sync(stocks, on_update).run()
