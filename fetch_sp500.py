"""Fetch S&P 500 constituents and screening metrics, write data/sp500.json.

Sources (both free, no API key):
  - Constituents and GICS sectors: Wikipedia "List of S&P 500 companies",
    falling back to the datasets/s-and-p-500-companies CSV on GitHub.
  - Metrics: Yahoo Finance via the yfinance library.

If a ticker fails to download, its previous row is kept (marked stale) so one
bad day doesn't drop stocks from the screener.
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sp500.json")
COLS = ["ticker", "name", "sector", "mktCap", "pe", "peg", "revGrowth", "netMargin", "divYield"]
UA = {"User-Agent": "Mozilla/5.0 (sp500-screener data job; github actions)"}


def constituents() -> pd.DataFrame:
    try:
        html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=UA, timeout=30).text
        df = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
        df = df.rename(columns={"Symbol": "ticker", "Security": "name", "GICS Sector": "sector"})
    except Exception as e:  # noqa: BLE001
        print(f"Wikipedia failed ({e}); using GitHub datasets CSV", file=sys.stderr)
        csv = requests.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
            headers=UA, timeout=30).text
        df = pd.read_csv(io.StringIO(csv)).rename(columns={"Symbol": "ticker", "Security": "name", "GICS Sector": "sector"})
    df = df[["ticker", "name", "sector"]].dropna(subset=["ticker"])
    df["ticker"] = df["ticker"].astype(str).str.strip()
    if len(df) < 450:
        raise SystemExit(f"Constituent list looks wrong: {len(df)} rows")
    return df


def num(v, scale: float = 1.0, nd: int = 2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f * scale, nd)


def metrics(info: dict) -> dict:
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    rate = info.get("dividendRate")
    if rate and price:
        div = num(rate / price, 100)
    else:
        div = num(info.get("trailingAnnualDividendYield"), 100)
    if div is None:
        div = 0.0 if info.get("marketCap") else None
    pe = num(info.get("trailingPE"), 1, 1)
    return {
        "mktCap": num(info.get("marketCap"), 1e-9, 1),      # billions USD
        "pe": pe if pe is None or pe > 0 else None,          # negative earnings -> n/a
        "peg": num(info.get("trailingPegRatio") or info.get("pegRatio")),
        "revGrowth": num(info.get("revenueGrowth"), 100, 1),  # most recent quarter, YoY %
        "netMargin": num(info.get("profitMargins"), 100, 1),
        "divYield": div,
    }


def fetch_one(sym: str, tries: int = 4) -> dict | None:
    ysym = sym.replace(".", "-")  # BRK.B -> BRK-B
    for i in range(tries):
        try:
            info = yf.Ticker(ysym).info or {}
            if info.get("marketCap") or info.get("trailingPE") or info.get("revenueGrowth") is not None:
                return metrics(info)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(2 ** i + random.random())
    return None


def main() -> None:
    prev = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT))
            prev = {r[0]: r for r in old.get("rows", [])}
        except Exception:  # noqa: BLE001
            pass

    df = constituents()
    rows, stale, failed = [], [], []
    for n, (sym, name, sector) in enumerate(df.itertuples(index=False), 1):
        m = fetch_one(sym)
        if m is None:
            if sym in prev:
                r = list(prev[sym]); r[1], r[2] = name, sector
                rows.append(r); stale.append(sym)
            else:
                failed.append(sym)
            continue
        rows.append([sym, name, sector] + [m[c] for c in COLS[3:]])
        if n % 50 == 0:
            print(f"{n}/{len(df)} fetched", flush=True)
        time.sleep(0.25)

    fresh = len(rows) - len(stale)
    if fresh < 0.8 * len(df):
        raise SystemExit(f"Only {fresh}/{len(df)} tickers fetched fresh; not overwriting data.")

    out = {
        "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance; constituents from Wikipedia",
        "universe": "S&P 500",
        "count": len(rows),
        "stale": stale,
        "cols": COLS,
        "rows": rows,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {len(rows)} rows ({len(stale)} stale, {len(failed)} missing) -> {OUT}")


if __name__ == "__main__":
    main()
