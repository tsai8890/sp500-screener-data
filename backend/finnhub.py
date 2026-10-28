"""Fast quotes from Finnhub for the stocks you are looking at.

Finnhub's free plan allows 60 requests a minute and one stock per request, so
it can't keep ~1,000 stocks fresh. Instead the page reports which stocks are on
screen (the one open in Analysis and the first 10 of the current watchlist),
and those few are fetched every FINNHUB_INTERVAL seconds (default 15) while a
page is open. Everything else keeps the regular yfinance schedule.

The API key is read from the environment or from a .env file next to the code
(FINNHUB_API_KEY=...), which git ignores. Without a key this module does nothing.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, time as dtime, timezone

import requests

from .common import MARKET_CLOSE, MARKET_OPEN, NY, iso, market_session

API = "https://finnhub.io/api/v1/quote"
INTERVAL = int(os.environ.get("FINNHUB_INTERVAL", "15"))
MAX_STOCKS = int(os.environ.get("FINNHUB_MAX_STOCKS", "11"))   # 11 every 15 s = 44 requests/min (limit 60)
AFTER_CLOSE = dtime(16, 1)
FOCUS_TTL = 45   # seconds a page's list counts after its last report

_focus: dict[str, float] = {}   # ticker -> last time a page asked for it
_lock = threading.Lock()
_pause_until = 0.0


def api_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "").strip()


def set_focus(tickers: list[str]) -> None:
    now = time.time()
    with _lock:
        for t in tickers[:MAX_STOCKS]:
            _focus[t] = now


def focus() -> list[str]:
    now = time.time()
    with _lock:
        for t in [t for t, at in _focus.items() if now - at > FOCUS_TTL]:
            del _focus[t]
        return sorted(_focus, key=lambda t: -_focus[t])[:MAX_STOCKS]


def quote(ticker: str, key: str) -> dict | None:
    """Finnhub /quote: {"c": current, "pc": previous close, "t": unix time of the last trade, ...}"""
    global _pause_until
    r = requests.get(API, params={"symbol": ticker, "token": key}, timeout=10)
    if r.status_code == 429:
        _pause_until = time.time() + 60
        print("Finnhub: rate limit reached, pausing fast quotes for 1 min.", file=sys.stderr, flush=True)
        return None
    if r.status_code in (401, 403):
        raise PermissionError("Finnhub rejected the API key (check FINNHUB_API_KEY in .env)")
    r.raise_for_status()
    q = r.json() or {}
    return q if q.get("c") else None   # unknown symbols come back as all zeros


def to_price(q: dict, now: datetime | None = None) -> tuple:
    """(regular price or None, extended (price, session, iso time) or None, previous close or None).

    During the regular session the quote is the regular price. Outside it, a trade
    timestamped after the close (or before the open) is an after-hours / pre-market
    price; a quote still showing the closing trade adds nothing, so yfinance's
    after-hours price is kept."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    price, prev, ts = q.get("c"), q.get("pc") or None, q.get("t")
    traded = datetime.fromtimestamp(ts, timezone.utc).astimezone(NY) if ts else None
    if market_session(now, grace_minutes=0):
        return price, None, prev
    if traded and traded.date() == now.date():
        tt = traded.time()
        if tt >= AFTER_CLOSE:   # the closing auction prints at 16:00:00; later trades are after hours
            return None, (price, "post", iso(traded.astimezone(timezone.utc))), None
        if tt < MARKET_OPEN:
            return None, (price, "pre", iso(traded.astimezone(timezone.utc))), None
    return None, None, None   # nothing newer than the close


def loop(on_quotes) -> None:
    """Every INTERVAL seconds, fetch the focus stocks and hand {ticker: (reg, ext, prev)} to on_quotes."""
    key = api_key()
    if not key:
        print("Finnhub: no FINNHUB_API_KEY (in .env or the environment); fast quotes are off.", flush=True)
        return
    print(f"Finnhub: fast quotes on, every {INTERVAL}s for up to {MAX_STOCKS} stocks you have open.", flush=True)
    while True:
        time.sleep(INTERVAL - (time.time() % INTERVAL))
        tickers = focus()
        if not tickers or time.time() < _pause_until or not market_session(extended=True):
            continue
        got = {}
        for t in tickers:
            try:
                q = quote(t, key)
            except PermissionError as e:
                print(f"Finnhub: {e}; fast quotes are off.", file=sys.stderr, flush=True)
                return
            except Exception as e:  # noqa: BLE001
                print(f"Finnhub: {t}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                continue
            if q:
                got[t] = to_price(q)
        if got:
            try:
                on_quotes(got)
            except Exception as e:  # noqa: BLE001
                print(f"Finnhub: couldn't apply quotes: {e}", file=sys.stderr, flush=True)


def load_dotenv(path: str) -> None:
    """KEY=value lines from .env into the environment (existing variables win)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass
