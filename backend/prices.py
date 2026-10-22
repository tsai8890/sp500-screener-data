"""Latest prices for every stock, fetched in parallel batches."""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yfinance as yf

from .common import FUNDAMENTALS_PATH, PRICES_PATH, iso, num, read_json, utc_now, write_json
from .universe import yahoo_symbol

BATCH = 50     # tickers per download call
WORKERS = 6    # batches downloaded at the same time


def _last_prices(frame: pd.DataFrame, symbols: list[str]) -> dict[str, float]:
    """Last non-empty close per symbol from a yf.download() frame."""
    if frame is None or frame.empty:
        return {}
    if isinstance(frame.columns, pd.MultiIndex):
        lvl0 = frame.columns.get_level_values(0)
        close = frame["Close"] if "Close" in lvl0 else frame.xs("Close", axis=1, level=1)
    else:  # single symbol, flat columns
        close = frame[["Close"]].rename(columns={"Close": symbols[0]})
    out = {}
    for sym in close.columns:
        s = close[sym].dropna()
        if len(s):
            v = num(s.iloc[-1], nd=4)
            if v and v > 0:
                out[str(sym)] = v
    return out


def _download(symbols: list[str]) -> dict[str, float]:
    for attempt in range(3):
        try:
            df = yf.download(symbols, period="1d", interval="1m", group_by="column",
                             auto_adjust=False, prepost=False, threads=False, progress=False)
            got = _last_prices(df, symbols)
            if got:
                return got
        except Exception as e:  # noqa: BLE001
            print(f"  batch {symbols[0]}..: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(1 + attempt)
    return {}


def update(tickers: list[str] | None = None, path: str = PRICES_PATH) -> dict:
    """Fetch the latest 1-minute close for every ticker and write prices.json.

    Tickers whose download fails keep their previous price."""
    started = time.time()
    if tickers is None:
        fund = read_json(FUNDAMENTALS_PATH) or {}
        tickers = fund.get("order") or list(fund.get("stocks", {}))
    if not tickers:
        raise RuntimeError("No tickers: run the fundamentals update first.")

    to_ticker = {yahoo_symbol(t): t for t in tickers}
    syms = list(to_ticker)
    batches = [syms[i:i + BATCH] for i in range(0, len(syms), BATCH)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        parts = list(pool.map(_download, batches))

    fresh = {}
    for part in parts:
        for sym, price in part.items():
            if sym in to_ticker:
                fresh[to_ticker[sym]] = price

    prev = (read_json(path) or {}).get("prices", {})
    prices = {t: fresh.get(t, prev.get(t)) for t in tickers if t in fresh or t in prev}
    if len(fresh) < 0.5 * len(tickers):
        raise RuntimeError(f"Only {len(fresh)}/{len(tickers)} prices fetched; keeping the previous file.")

    out = {"asOf": iso(utc_now()), "fresh": len(fresh), "prices": prices}
    write_json(path, out)
    print(f"Prices: {len(fresh)}/{len(tickers)} fresh in {time.time() - started:.1f}s -> {path}")
    return out
