"""Per-stock fundamentals from Yahoo Finance, fetched in parallel.

These change slowly (earnings, revenue growth, margins, dividends), so they are
refreshed every 30 minutes. Anything that depends on the share price (P/E, PEG,
market cap, dividend yield) is recomputed from the latest price in build.py.
"""
from __future__ import annotations

import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

from .common import FUNDAMENTALS_PATH, iso, num, read_json, utc_now, write_json
from . import universe
from .universe import yahoo_symbol

WORKERS = 8         # parallel requests in the first pass; higher values get rate-limited by Yahoo
RETRY_WORKERS = 2   # slower second pass for the stocks that failed


def extract(info: dict) -> dict | None:
    """Pick the fields the screener needs from a yfinance .info dict."""
    price = num(info.get("currentPrice") or info.get("regularMarketPrice"), nd=None)
    cap = num(info.get("marketCap"), nd=None)
    if not price or not cap:
        return None
    pe = num(info.get("trailingPE"), nd=None)
    peg = num(info.get("trailingPegRatio") or info.get("pegRatio"), nd=None)
    eps = num(info.get("trailingEps"), nd=None)
    if eps is None and pe and pe > 0:
        eps = price / pe
    # Yahoo's PEG = P/E / expected EPS growth (%). Keep the implied growth so
    # PEG can be recomputed whenever the price moves.
    growth = pe / peg if pe and pe > 0 and peg and peg > 0 else None
    div_rate = num(info.get("dividendRate"), nd=None)
    if div_rate is None:
        ty = num(info.get("trailingAnnualDividendYield"), nd=None)
        div_rate = ty * price if ty else 0.0
    return {
        "shares": cap / price,                       # implied shares, consistent with Yahoo's market cap
        "eps": num(eps, nd=4),
        "growth": num(growth, nd=3),                 # % per year, implied by Yahoo's PEG
        "revGrowth": num(info.get("revenueGrowth"), 100, 1),
        "netMargin": num(info.get("profitMargins"), 100, 1),
        "divRate": num(div_rate, nd=4),
        "price": num(price, nd=4),                   # price when these fundamentals were read
        "prevClose": num(info.get("regularMarketPreviousClose") or info.get("previousClose"), nd=4),
    }


def fetch_one(ticker: str, tries: int = 4) -> dict | None:
    sym = yahoo_symbol(ticker)
    for i in range(tries):
        try:
            got = extract(yf.Ticker(sym).info or {})
            if got:
                return got
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(2 ** i + random.random())
    return None


def update(path: str = FUNDAMENTALS_PATH, workers: int = WORKERS) -> dict:
    """Fetch fundamentals for every stock in any universe and write fundamentals.json.

    A stock that fails keeps its previous values (listed under "stale"). If
    fewer than 80% come back fresh, nothing is written and RuntimeError is raised.
    """
    started = time.time()
    prev = (read_json(path) or {}).get("stocks", {})
    stocks_in = universe.union(universe.load())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda row: fetch_one(row[0]), stocks_in))

    # Second, gentler pass for whatever failed (usually Yahoo rate limiting).
    failed = [i for i, got in enumerate(results) if got is None]
    if failed:
        time.sleep(10)
        with ThreadPoolExecutor(max_workers=RETRY_WORKERS) as pool:
            retried = list(pool.map(lambda i: fetch_one(stocks_in[i][0], tries=3), failed))
        for i, got in zip(failed, retried):
            results[i] = got
        print(f"Retry pass: {sum(g is not None for g in retried)}/{len(failed)} recovered")

    stocks, stale, missing = {}, [], []
    for (ticker, name, sector), got in zip(stocks_in, results):
        if got is None:
            if ticker in prev:
                stocks[ticker] = {**prev[ticker], "name": name, "sector": sector}
                stale.append(ticker)
            else:
                missing.append(ticker)
            continue
        stocks[ticker] = {"name": name, "sector": sector, **got}

    fresh = len(stocks) - len(stale)
    if fresh < 0.8 * len(stocks_in):
        raise RuntimeError(f"Only {fresh}/{len(stocks_in)} stocks fetched fresh; keeping the previous file.")

    out = {
        "asOf": iso(utc_now()),
        "order": [t for t, _, _ in stocks_in if t in stocks],
        "stale": stale,
        "missing": missing,
        "stocks": stocks,
    }
    write_json(path, out)
    print(f"Fundamentals: {fresh} fresh, {len(stale)} stale, {len(missing)} missing "
          f"in {time.time() - started:.0f}s -> {path}")
    return out
