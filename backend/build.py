"""Merge fundamentals and the latest prices into data/sp500.json (what the page reads)."""
from __future__ import annotations

from .common import FUNDAMENTALS_PATH, PRICES_PATH, SP500_PATH, iso, num, read_json, utc_now, write_json

COLS = ["ticker", "name", "sector", "price", "mktCap", "pe", "peg", "revGrowth", "netMargin", "divYield"]


def row(ticker: str, f: dict, price: float | None) -> list:
    price = price or f.get("price")
    eps, growth = f.get("eps"), f.get("growth")
    pe = price / eps if price and eps and eps > 0 else None
    peg = pe / growth if pe and growth and growth > 0 else None
    cap = f["shares"] * price / 1e9 if price and f.get("shares") else None
    div = f["divRate"] / price * 100 if price and f.get("divRate") else (0.0 if price else None)
    return [ticker, f.get("name", ""), f.get("sector", ""),
            num(price, nd=2), num(cap, nd=1), num(pe, nd=1), num(peg, nd=2),
            f.get("revGrowth"), f.get("netMargin"), num(div, nd=2)]


def combine(fund: dict, prices: dict | None) -> dict:
    stocks = fund.get("stocks", {})
    order = fund.get("order") or list(stocks)
    live = (prices or {}).get("prices", {})
    rows = [row(t, stocks[t], live.get(t)) for t in order if t in stocks]
    return {
        "asOf": (prices or {}).get("asOf") or fund.get("asOf"),   # time of the newest prices
        "pricesAsOf": (prices or {}).get("asOf"),
        "fundamentalsAsOf": fund.get("asOf"),
        "builtAt": iso(utc_now()),
        "source": "Yahoo Finance via yfinance; constituents from Wikipedia",
        "universe": "S&P 500",
        "count": len(rows),
        "stale": fund.get("stale", []),
        "cols": COLS,
        "rows": rows,
    }


def build(path: str = SP500_PATH, fund: dict | None = None, prices: dict | None = None) -> dict:
    fund = fund or read_json(FUNDAMENTALS_PATH)
    if not fund:
        raise RuntimeError("No fundamentals yet: run the fundamentals update first.")
    out = combine(fund, prices if prices is not None else read_json(PRICES_PATH))
    write_json(path, out)
    return out
