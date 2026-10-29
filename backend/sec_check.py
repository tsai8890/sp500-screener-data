"""Compare one company's SEC and Yahoo figures side by side (needs SEC_USER_AGENT in .env).

  python -m backend.sec_check GOOG
  python -m backend.sec_check GOOG --annual

Shows the last four quarters (or fiscal years) for the main line items, from each
source, and how far apart they are. Large gaps usually mean the two define an item
differently (e.g. total debt: the SEC figure here is borrowings only, Yahoo's
includes leases) or that a company files under a tag this code doesn't know yet.
"""
from __future__ import annotations

import argparse
import os
import sys

from .common import ROOT, iso, load_dotenv, utc_now
from .sources import sec, sec_xbrl, yahoo
from .statements import merge

LINES = [("income", "revenue"), ("income", "grossProfit"), ("income", "operatingIncome"), ("income", "netIncome"),
         ("income", "eps"), ("cashflow", "operatingCF"), ("cashflow", "capex"), ("cashflow", "fcf"),
         ("balance", "cash"), ("balance", "totalDebt"), ("balance", "equity")]


def _fmt(v, key):
    if v is None:
        return "–"
    if key == "eps":
        return f"{v:.2f}"
    return f"{v / 1e9:,.2f}B"


def main(argv=None) -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser(prog="python -m backend.sec_check")
    ap.add_argument("ticker")
    ap.add_argument("--annual", action="store_true", help="fiscal years instead of quarters")
    args = ap.parse_args(argv)
    t = args.ticker.upper().replace("-", ".")
    view = "annual" if args.annual else "quarterly"
    if not sec.enabled():
        print("Set SEC_USER_AGENT in .env first (your name and email).")
        return 1
    cik = sec.cik_for(t)
    if not cik:
        print(f"The SEC doesn't list {t}.")
        return 1
    s = sec_xbrl.statements(sec.company_facts(cik, priority=True), t, iso(utc_now()))
    try:
        y = yahoo.fetch_statements(t)
    except Exception as e:  # noqa: BLE001
        print(f"(Yahoo: {type(e).__name__}: {e})")
        y = {"annual": {}, "quarterly": {}}
    print(f"{s.get('entityName')} (CIK {cik}), newest SEC filing {s.get('lastFiled')}")
    # periods: the newest four, matched by date across the two sources
    ends = sorted(merge([s, y])[view]["income"])[-4:]
    sec_m, y_m = merge([s])[view], merge([y])[view]

    def val(m, kind, key, end):
        from datetime import date
        d = date.fromisoformat(end)
        for e, rec in m[kind].items():
            if abs((date.fromisoformat(e) - d).days) <= 7 and key in rec:
                return rec[key]
        return None

    for end in ends:
        print(f"\n{'year' if view == 'annual' else 'quarter'} ending {end}")
        print(f"  {'':16}{'SEC':>12}{'Yahoo':>12}{'diff':>9}")
        for kind, key in LINES:
            a, b = val(sec_m, kind, key, end), val(y_m, kind, key, end)
            diff = f"{(a - b) / abs(b) * 100:+.1f}%" if a is not None and b else ""
            print(f"  {key:16}{_fmt(a, key):>12}{_fmt(b, key):>12}{diff:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
