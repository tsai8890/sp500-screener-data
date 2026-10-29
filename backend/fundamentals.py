"""Per-stock fundamentals from Yahoo Finance, fetched in parallel.

These change slowly (earnings, revenue growth, margins, dividends), so they are
refreshed every few hours, and only for stocks whose data is older than that. Anything that depends on the share price (P/E, PEG,
market cap, dividend yield) is recomputed from the latest price in build.py.
"""
from __future__ import annotations

import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import yfinance as yf

from .common import FUNDAMENTALS_PATH, iso, num, parse_iso, read_json, utc_now, write_json
from . import universe
from .universe import yahoo_symbol
from .sources.yahoo import GATE, is_rate_limit

WORKERS = 3   # threads; the request rate itself is set by the shared Yahoo gate (YAHOO_RPS)
CHUNK = 100   # stocks fetched between saves
BLOCKED_MIN = 20   # this many stocks all failing at once means Yahoo is blocking us, not bad tickers


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
        "fwdEps": num(info.get("forwardEps"), nd=4),   # analysts' consensus EPS, next 12 months
        "growth": num(growth, nd=3),                 # % per year, implied by Yahoo's PEG
        "revGrowth": num(info.get("revenueGrowth"), 100, 1),
        "netMargin": num(info.get("profitMargins"), 100, 1),
        "divRate": num(div_rate, nd=4),
        "price": num(price, nd=4),                   # price when these fundamentals were read
        "prevClose": num(info.get("regularMarketPreviousClose") or info.get("previousClose"), nd=4),
    }


def fetch_one(ticker: str, tries: int = 3) -> dict | None:
    """One stock's fundamentals, paced by the shared Yahoo gate. None if unavailable."""
    sym = yahoo_symbol(ticker)
    failures = limits = 0
    while failures < tries and limits < 6:
        GATE.wait()
        try:
            got = extract(yf.Ticker(sym).info or {})
            GATE.ok()
            if got:
                return got
            failures += 1          # Yahoo answered but has no usable data
        except Exception as e:  # noqa: BLE001
            if is_rate_limit(e):
                limits += 1
                GATE.limited()     # every thread waits out the pause, then this one tries again
                continue
            failures += 1
            if failures == tries:
                print(f"  {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(1 + random.random())
    return None


def _fetched_at(rec: dict | None, file_asof: str | None) -> datetime | None:
    if not rec:
        return None
    return parse_iso(rec.get("fetchedAt") or file_asof)


def due(prev: dict | None, max_age: float | None = None, stocks_in: list | None = None) -> list:
    """Stocks (ticker, name, sector) whose fundamentals are missing or older than max_age
    seconds, oldest first. max_age None: all of them. A stock Yahoo had no data for
    is tried again only after max_age too, so a few dead tickers aren't retried all day."""
    stocks_in = stocks_in if stocks_in is not None else universe.union(universe.load())
    if max_age is None:
        return list(stocks_in)
    prev = prev or {}
    have, asof, now = prev.get("stocks", {}), prev.get("asOf"), utc_now()
    failed_at = prev.get("failedAt") or {}
    out = []
    for row in stocks_in:
        f = parse_iso(failed_at.get(row[0]))
        if f and (now - f).total_seconds() < max_age:
            continue
        t = _fetched_at(have.get(row[0]), asof)
        age = (now - t).total_seconds() if t else float("inf")
        if age >= max_age:
            out.append((age, row))
    out.sort(key=lambda x: -x[0])
    return [row for _, row in out]


def update(path: str = FUNDAMENTALS_PATH, workers: int = WORKERS, max_age: float | None = None,
           on_progress=None) -> dict:
    """Fetch fundamentals and write fundamentals.json.

    Only stocks that are missing or older than max_age seconds are fetched (all of
    them when max_age is None), oldest first. The file is written after every
    CHUNK stocks, so an interrupted run keeps its progress and the page fills in
    as it goes (on_progress(out) is called each time). A stock that fails keeps
    its previous values. "pending" in the result counts stocks still due.
    """
    started = time.time()
    prev = read_json(path) or {}
    prev_stocks, prev_asof = prev.get("stocks", {}), prev.get("asOf")
    failed_at: dict[str, str] = dict(prev.get("failedAt") or {})
    stocks_in = universe.union(universe.load())
    todo = due(prev, max_age, stocks_in)
    fresh: dict[str, dict] = {}
    failed: set[str] = set()
    out = prev

    def merged() -> dict:
        stocks, stale, missing = {}, [], []
        for ticker, name, sector in stocks_in:
            if ticker in fresh:
                stocks[ticker] = {"name": name, "sector": sector, **fresh[ticker]}
            elif ticker in prev_stocks:
                rec = dict(prev_stocks[ticker])
                rec.setdefault("fetchedAt", prev_asof)
                stocks[ticker] = {**rec, "name": name, "sector": sector}
                if ticker in failed:
                    stale.append(ticker)
            else:
                missing.append(ticker)
        return {
            "asOf": iso(utc_now()) if fresh else prev_asof or iso(utc_now()),
            "order": [t for t, _, _ in stocks_in if t in stocks],
            "stale": stale,
            "missing": missing,
            "pending": len(todo) - len(fresh) - len(failed),
            "failedAt": {t: v for t, v in failed_at.items() if t not in fresh},
            "stocks": stocks,
        }

    if todo:
        from .sources.yahoo import RPS
        mins = max(1, round(len(todo) / max(RPS, 0.01) / 60))
        print(f"Fundamentals: fetching {len(todo)} stocks from Yahoo Finance (about {mins} min; "
              f"progress every {CHUNK})...", flush=True)
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda row: fetch_one(row[0]), chunk))
        stamp = iso(utc_now())
        for (ticker, _, _), got in zip(chunk, results):
            if got is None:
                failed.add(ticker)
                failed_at[ticker] = stamp
            else:
                fresh[ticker] = {**got, "fetchedAt": stamp}
        out = merged()
        if out["stocks"]:
            write_json(path, out)
            if on_progress:
                on_progress(out)
        print(f"Fundamentals: {len(fresh)}/{len(todo)} fetched ({len(failed)} failed) "
              f"after {time.time() - started:.0f}s", flush=True)

    if len(todo) >= BLOCKED_MIN and not fresh:
        raise RuntimeError(f"None of {len(todo)} stocks could be fetched from Yahoo (it may be blocking this "
                           f"computer for a while); keeping the previous data and trying again later.")
    if failed:
        names = ", ".join(sorted(failed)[:12]) + (" …" if len(failed) > 12 else "")
        print(f"Fundamentals: no data from Yahoo for {len(failed)} stock(s): {names}. "
              f"They keep their previous values and are tried again in a few hours.", flush=True)
    if not todo:
        out = merged()
    return out
