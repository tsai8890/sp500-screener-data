"""Latest prices for every stock, fetched in parallel batches."""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yfinance as yf

from datetime import timezone

from .common import (FUNDAMENTALS_PATH, MARKET_CLOSE, MARKET_OPEN, NY, PRICES_PATH, iso, market_session, num,
                     read_json, utc_now, write_json)
from .universe import yahoo_symbol
from .yahoo import GATE, is_rate_limit

_file_lock = threading.Lock()   # prices.json is written by the schedule and by single-stock refreshes
BATCH = 50     # tickers per download call
WORKERS = 2    # batches downloaded at the same time (each ticker is its own Yahoo request)


def _close_frame(frame: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        lvl0 = frame.columns.get_level_values(0)
        return frame["Close"] if "Close" in lvl0 else frame.xs("Close", axis=1, level=1)
    return frame[["Close"]].rename(columns={"Close": symbols[0]})   # single symbol, flat columns


def _is_regular(ts) -> bool:
    t = ts.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


def _split(series: pd.Series):
    """(last regular-session price, (extended-hours price, "pre"/"post", iso time) or None).

    The extended price is the last trade after the latest regular-session bar:
    after hours the same day, or pre-market the next day. While the market is
    open the latest bar is a regular one, so there is none."""
    s = series.dropna()
    s = s[s > 0]
    if not len(s):
        return None, None
    if not isinstance(s.index, pd.DatetimeIndex):
        return num(s.iloc[-1], nd=4), None          # no timestamps: treat everything as regular
    idx = s.index.tz_convert(NY) if s.index.tz is not None else s.index.tz_localize(NY)
    s = pd.Series(s.values, index=idx)
    reg_mask = [_is_regular(t) for t in idx]
    reg = s[reg_mask]
    if not len(reg):
        return None, (num(s.iloc[-1], nd=4), "pre" if idx[-1].time() < MARKET_OPEN else "post", iso(idx[-1].astimezone(timezone.utc)))
    last_reg_t = reg.index[-1]
    after = s[s.index > last_reg_t]
    ext = None
    if len(after):
        t = after.index[-1]
        session = "post" if t.date() == last_reg_t.date() and t.time() >= MARKET_CLOSE else "pre"
        ext = (num(after.iloc[-1], nd=4), session, iso(t.astimezone(timezone.utc)))
    return num(reg.iloc[-1], nd=4), ext


def _parse(frame: pd.DataFrame, symbols: list[str]) -> dict[str, tuple]:
    """{symbol: (regular price or None, extended (price, session, time) or None)}"""
    if frame is None or frame.empty:
        return {}
    close = _close_frame(frame, symbols)
    out = {}
    for sym in close.columns:
        reg, ext = _split(close[sym])
        if reg is not None or ext is not None:
            out[str(sym)] = (reg, ext)
    return out


def _last_prices(frame: pd.DataFrame, symbols: list[str]) -> dict[str, float]:
    """Last regular-session close per symbol from a yf.download() frame."""
    return {k: v[0] for k, v in _parse(frame, symbols).items() if v[0] is not None}


def _rate_limited_in_last_download() -> bool:
    errors = getattr(getattr(yf, "shared", None), "_ERRORS", None) or {}
    return any(is_rate_limit(v) for v in list(errors.values()))


def _download(symbols: list[str], priority: bool = False) -> dict[str, tuple]:
    """{symbol: (regular price, extended-hours (price, session, time) or None)}"""
    # with pre/post-market bars; outside regular hours ask for two days so the
    # last regular close is always included (before the open, today has none yet)
    period = "1d" if market_session(grace_minutes=0) else "2d"
    for attempt in range(3):
        GATE.wait(priority=priority)
        if GATE.paused_for():
            if priority:
                return {}   # someone is waiting on the page: don't sit out a rate-limit pause
            continue
        try:
            df = yf.download(symbols, period=period, interval="1m", group_by="column",
                             auto_adjust=False, prepost=True, ignore_tz=False, threads=False, progress=False)
            got = _parse(df, symbols)
            if _rate_limited_in_last_download():
                GATE.limited()
            elif got:
                GATE.ok()
            if got:
                return got
        except Exception as e:  # noqa: BLE001
            if is_rate_limit(e):
                GATE.limited()
            else:
                print(f"  batch {symbols[0]}..: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(1 + attempt)
    return {}


def update(tickers: list[str] | None = None, path: str = PRICES_PATH) -> dict:
    """Fetch the latest 1-minute prices for every ticker and write prices.json.

    "prices": last regular-session price per ticker (used for P/E, market cap ...).
    "ext": latest pre-market / after-hours trade, [price, "pre"|"post", time], for
    tickers that have one. Tickers whose download fails keep their previous values."""
    started = time.time()
    if tickers is None:
        fund = read_json(FUNDAMENTALS_PATH) or {}
        tickers = fund.get("order") or list(fund.get("stocks", {}))
    if not tickers:
        raise RuntimeError("No tickers: run the fundamentals update first.")

    to_ticker = {yahoo_symbol(t): t for t in tickers}
    syms = list(to_ticker)
    batches = [syms[i:i + BATCH] for i in range(0, len(syms), BATCH)]
    print(f"Prices: fetching {len(syms)} stocks from Yahoo Finance (incl. pre-market / after-hours)...", flush=True)
    done = [0]

    def run(batch):
        got = _download(batch)
        done[0] += len(batch)
        if len(batches) > 4 and done[0] // BATCH % 5 == 0:   # a progress line every 250 stocks
            print(f"Prices: {done[0]}/{len(syms)} ...", flush=True)
        return got

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        parts = list(pool.map(run, batches))

    fresh, ext_fresh, got = {}, {}, set()
    for part in parts:
        for sym, val in part.items():
            if sym not in to_ticker:
                continue
            t = to_ticker[sym]
            reg, ext = val if isinstance(val, tuple) else (val, None)
            got.add(t)
            if reg is not None:
                fresh[t] = reg
            if ext is not None:
                ext_fresh[t] = list(ext)

    with _file_lock:
        return _merge_and_write(path, tickers, fresh, ext_fresh, got, started)


def _merge_and_write(path, tickers, fresh, ext_fresh, got, started) -> dict:
    prev_file = read_json(path) or {}
    prev, prev_ext = prev_file.get("prices", {}), prev_file.get("ext", {})
    prices = {t: fresh.get(t, prev.get(t)) for t in tickers if t in fresh or t in prev}
    # a stock that came back has exactly the extended price Yahoo gave (none while the market is open);
    # one that failed keeps what it had
    ext = {t: (ext_fresh[t] if t in got else prev_ext[t]) for t in tickers
           if (t in got and t in ext_fresh) or (t not in got and t in prev_ext)}
    if len(got) < 0.5 * len(tickers):
        raise RuntimeError(f"Only {len(got)}/{len(tickers)} prices fetched; keeping the previous file.")

    out = {"asOf": iso(utc_now()), "fresh": len(fresh), "prices": prices, "ext": ext}
    write_json(path, out)
    print(f"Prices: done. {len(fresh)}/{len(tickers)} fresh, {len(ext)} with a pre-market/after-hours price, "
          f"in {time.time() - started:.1f}s -> {path}")
    return out


def update_one(ticker: str, path: str = PRICES_PATH) -> dict | None:
    """Fetch the latest price of one stock right away (the page's Refresh button) and
    merge it into prices.json. Returns the updated file, or None if Yahoo gave nothing."""
    sym = yahoo_symbol(ticker)
    got = _download([sym], priority=True).get(sym)
    if not got:
        return None
    reg, ext = got
    with _file_lock:
        data = read_json(path) or {"prices": {}, "ext": {}}
        if reg is not None:
            data.setdefault("prices", {})[ticker] = reg
        data.setdefault("ext", {})
        if ext is not None:
            data["ext"][ticker] = list(ext)
        else:
            data["ext"].pop(ticker, None)
        write_json(path, data)
    return data
