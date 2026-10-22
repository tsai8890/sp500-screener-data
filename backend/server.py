"""Always-on screener server (Python standard library only, plus the backend's own deps).

  python -m backend.server

What it does:
  * prices: every minute while the US market is open (and 15 min after the close)
  * fundamentals: every 30 minutes
  * after each update, rebuilds data/sp500.json and keeps it in memory
  * serves the page and the data:
        GET /                  the screener page (index.html)
        GET /api/sp500         latest combined data (JSON, never cached)
        GET /api/health        update times, last errors
        GET /data/sp500.json   same data as a file, for the page's fallback

Settings (environment variables):
  HOST (0.0.0.0)  PORT (8000)
  ALLOWED_ORIGINS  comma-separated origins allowed to call the API from a browser, or * (default)
  PRICE_INTERVAL (60 s)  FUNDAMENTALS_INTERVAL (1800 s)
  PRICES_ALWAYS (0)  set to 1 to keep fetching prices when the market is closed
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import build, fundamentals, prices
from .common import (FUNDAMENTALS_PATH, PRICES_PATH, ROOT, SP500_PATH, iso, market_session,
                     parse_iso, read_json, utc_now)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
PRICE_INTERVAL = int(os.environ.get("PRICE_INTERVAL", "60"))
FUND_INTERVAL = int(os.environ.get("FUNDAMENTALS_INTERVAL", "1800"))
PRICES_ALWAYS = os.environ.get("PRICES_ALWAYS", "0") == "1"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fund = read_json(FUNDAMENTALS_PATH)
        self.prices = read_json(PRICES_PATH)
        self.payload = b""
        self.health = {"startedAt": iso(utc_now()), "fundamentals": {}, "prices": {}}
        data = read_json(SP500_PATH)
        if data:
            self.payload = json.dumps(data, separators=(",", ":")).encode()

    def rebuild(self) -> None:
        if not self.fund:
            return
        out = build.build(fund=self.fund, prices=self.prices)
        with self.lock:
            self.payload = json.dumps(out, separators=(",", ":")).encode()

    def record(self, job: str, ok: bool, seconds: float, err: str | None = None) -> None:
        h = self.health[job]
        h["lastRun"] = iso(utc_now())
        h["lastSeconds"] = round(seconds, 1)
        if ok:
            h["lastSuccess"] = h["lastRun"]
            h.pop("lastError", None)
        else:
            h["lastError"] = err


STATE = State()


def _age(asof: str | None) -> float:
    dt = parse_iso(asof)
    return (utc_now() - dt).total_seconds() if dt else float("inf")


def fundamentals_loop() -> None:
    while True:
        due = FUND_INTERVAL - _age((STATE.fund or {}).get("asOf"))
        if due > 0:
            time.sleep(min(due, 60))
            continue
        t0 = time.time()
        try:
            STATE.fund = fundamentals.update()
            STATE.rebuild()
            STATE.record("fundamentals", True, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            STATE.record("fundamentals", False, time.time() - t0, f"{type(e).__name__}: {e}")
            time.sleep(120)  # back off before retrying


def prices_loop() -> None:
    while True:
        # wake on the minute boundary
        time.sleep(PRICE_INTERVAL - (time.time() % PRICE_INTERVAL))
        if not STATE.fund:
            continue
        if not (PRICES_ALWAYS or market_session() or STATE.prices is None):
            continue
        t0 = time.time()
        try:
            STATE.prices = prices.update(STATE.fund.get("order"))
            STATE.rebuild()
            STATE.record("prices", True, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            STATE.record("prices", False, time.time() - t0, f"{type(e).__name__}: {e}")


STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "sp500-screener"

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if "*" in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/api/sp500", "/data/sp500.json"):
            with STATE.lock:
                body = STATE.payload
            if not body:
                return self._send(503, b'{"error":"No data yet, first update still running"}', "application/json")
            return self._send(200, body, "application/json")
        if path == "/api/health":
            fund, pr = STATE.fund or {}, STATE.prices or {}
            h = {**STATE.health, "marketOpen": market_session(grace_minutes=0),
                 "fundamentalsAsOf": fund.get("asOf"), "pricesAsOf": pr.get("asOf"),
                 "stocks": len(fund.get("stocks", {}))}
            return self._send(200, json.dumps(h, indent=2).encode(), "application/json")
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                with open(os.path.join(ROOT, name), "rb") as f:
                    return self._send(200, f.read(), ctype, cache="no-cache")
            except OSError:
                pass
        self._send(404, b"Not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt, *args) -> None:  # quieter logs: only errors
        if len(args) < 2 or str(args[1])[:1] in "45":
            super().log_message(fmt, *args)


def main() -> None:
    threading.Thread(target=fundamentals_loop, name="fundamentals", daemon=True).start()
    threading.Thread(target=prices_loop, name="prices", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Screener server on http://{HOST}:{PORT}  (prices every {PRICE_INTERVAL}s while the "
          f"market is open, fundamentals every {FUND_INTERVAL}s)", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
