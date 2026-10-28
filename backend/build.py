"""Merge fundamentals and the latest prices into one data file per universe.

Writes data/<universe>.json (e.g. data/sp500.json, data/russell1000.json),
which the page reads, plus data/index.json listing the universes.
"""
from __future__ import annotations

from .common import (FUNDAMENTALS_PATH, INDEX_PATH, PRICES_PATH, UNIVERSES_PATH, iso, num, read_json,
                     universe_path, utc_now, write_json)
from .universe import DEFAULT, UNIVERSES

COLS = ["ticker", "name", "sector", "price", "chg", "mktCap", "pe", "fpe", "peg", "revGrowth", "netMargin", "divYield",
        "xPrice", "xChg", "xSession"]   # pre-market / after-hours: price, % vs the regular close, "pre" or "post"


def row(ticker: str, f: dict, price: float | None, ext: list | None = None, prev_close: float | None = None) -> list:
    price = price or f.get("price")
    # valuation uses the latest trade: the pre-market / after-hours price when there is one
    last = ext[0] if ext and ext[0] else price
    eps, growth = f.get("eps"), f.get("growth")
    pe = last / eps if last and eps and eps > 0 else None
    fwd = f.get("fwdEps")
    fpe = last / fwd if last and fwd and fwd > 0 else None       # forward P/E
    peg = pe / growth if pe and growth and growth > 0 else None
    cap = f["shares"] * last / 1e9 if last and f.get("shares") else None
    div = f["divRate"] / last * 100 if last and f.get("divRate") else (0.0 if last else None)
    prev = prev_close or f.get("prevClose")   # from the price data when we have it (always current)
    chg = (price / prev - 1) * 100 if price and prev else None   # % vs previous session's close
    return [ticker, f.get("name", ""), f.get("sector", ""),
            num(price, nd=2), num(chg, nd=2), num(cap, nd=1), num(pe, nd=1), num(fpe, nd=1), num(peg, nd=2),
            f.get("revGrowth"), f.get("netMargin"), num(div, nd=2), *_ext(ext, price)]


def _ext(ext: list | None, price: float | None) -> list:
    if not ext or not ext[0] or not price:
        return [None, None, None]
    return [num(ext[0], nd=2), num((ext[0] / price - 1) * 100, nd=2), ext[1]]


def combine(fund: dict, prices: dict | None, tickers: list[str] | None = None,
            uid: str = DEFAULT, name: str = "S&P 500") -> dict:
    stocks = fund.get("stocks", {})
    order = tickers if tickers is not None else (fund.get("order") or list(stocks))
    live = (prices or {}).get("prices", {})
    ext = (prices or {}).get("ext", {})
    prevs = (prices or {}).get("prev", {})
    rows = [row(t, stocks[t], live.get(t), ext.get(t), prevs.get(t)) for t in order if t in stocks]
    return {
        "asOf": (prices or {}).get("asOf") or fund.get("asOf"),   # time of the newest prices
        "pricesAsOf": (prices or {}).get("asOf"),
        "fundamentalsAsOf": fund.get("asOf"),
        "builtAt": iso(utc_now()),
        "source": "Yahoo Finance via yfinance; index constituents from Wikipedia",
        "universeId": uid,
        "universe": name,
        "count": len(rows),
        "stale": [t for t in fund.get("stale", []) if t in set(order)],
        "cols": COLS,
        "rows": rows,
    }


def build(fund: dict | None = None, prices: dict | None = None, universes: dict | None = None) -> dict[str, dict]:
    """Write one data file per universe; returns {universe id: data}."""
    fund = fund or read_json(FUNDAMENTALS_PATH)
    if not fund:
        raise RuntimeError("No fundamentals yet: run the fundamentals update first.")
    prices = prices if prices is not None else read_json(PRICES_PATH)
    lists = (universes or read_json(UNIVERSES_PATH) or {}).get("universes", {})
    if not lists:  # no cached lists yet: everything we have is the default universe
        lists = {DEFAULT: {"name": UNIVERSES[DEFAULT][0], "stocks": [[t] for t in fund.get("order", [])]}}
    outs, index = {}, []
    for uid in [u for u in UNIVERSES if u in lists]:
        meta = lists[uid]
        out = combine(fund, prices, [s[0] for s in meta["stocks"]], uid, meta.get("name", uid))
        write_json(universe_path(uid), out)
        outs[uid] = out
        index.append({"id": uid, "name": out["universe"], "count": out["count"], "file": f"data/{uid}.json"})
    write_json(INDEX_PATH, {"default": DEFAULT, "builtAt": iso(utc_now()), "universes": index})
    return outs
