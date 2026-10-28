"""Local screener server (Python standard library only, plus the backend's own deps).

  python -m backend.server        (or ./run.sh)

Everything stays on this machine: market data in data/*.json, saved screens and
watchlists in SQLite (data/screener.db), notes as Markdown files (data/notes/*.md).

What it does:
  * prices: every 5 minutes from 4:00 to 20:00 New York time on weekdays, i.e. the
    regular session plus pre-market and after-hours (shown separately on the page)
  * fundamentals: each stock refreshed every 6 hours (they change at most once a quarter)
  * all Yahoo requests share one pace (YAHOO_RPS) and pause automatically when Yahoo
    answers "Too Many Requests" (backend/yahoo.py)
  * after each update, rebuilds data/sp500.json and keeps it in memory
  * serves the page and the data:
        GET /                  the screener page (index.html)
        GET /api/data/<id>     latest data for one universe: sp500, russell1000 (JSON, never cached)
        GET /api/index         the universes available
        GET /api/sp500         same as /api/data/sp500 (kept for older pages)
        GET /api/health        update times, last errors
        GET /data/<id>.json    same data as a file, for the page's fallback
        GET /data/screens.json built-in screens
        GET    /api/screens          saved screens (SQLite, data/screener.db)
        PUT    /api/screens/<id>     create or replace one screen (JSON body)
        DELETE /api/screens/<id>     delete one screen
        GET    /api/watchlists       saved watchlists (same database)
        PUT    /api/watchlists/<id>  create or replace one watchlist
        DELETE /api/watchlists/<id>  delete one watchlist
        GET    /api/notes            Markdown notes (files in data/notes/)
        PUT    /api/notes/<id>       save one note: {body, pinned, created, updated}
        DELETE /api/notes/<id>       delete one note
        GET /api/storage           where screens, watchlists and notes are kept
        GET /api/analysis/<ticker> financial statements and metrics for one stock
                                   (cached a day in data/analysis/; ?refresh=1 to refetch)

Settings (environment variables):
  HOST (127.0.0.1: this computer only; 0.0.0.0 to allow other devices)  PORT (8000)
  ALLOWED_ORIGINS  comma-separated origins allowed to call the API from a browser, or * (default)
  PRICE_INTERVAL (300 s)  FUNDAMENTALS_INTERVAL (21600 s = 6 h)  YAHOO_RPS (1 request/second)
  PRICES_ALWAYS (0)  set to 1 to keep fetching prices when the market is closed
  SCREENS_TOKEN  if set, PUT/DELETE on /api/screens need "Authorization: Bearer <token>"
  SCREENER_DB  SQLite file for saved screens and watchlists (data/screener.db)
  SCREENER_NOTES_DIR  folder for notes (data/notes)
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import analysis, build, fundamentals, notes_store, prices, screens_db
from .common import (FUNDAMENTALS_PATH, INDEX_PATH, PRICES_PATH, ROOT, iso, market_session,
                     parse_iso, read_json, universe_path, utc_now)
from .universe import UNIVERSES
from .yahoo import GATE

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
PRICE_INTERVAL = int(os.environ.get("PRICE_INTERVAL", "300"))
FUND_INTERVAL = int(os.environ.get("FUNDAMENTALS_INTERVAL", "21600"))
PRICES_ALWAYS = os.environ.get("PRICES_ALWAYS", "0") == "1"
SCREENS_TOKEN = os.environ.get("SCREENS_TOKEN", "")
MAX_BODY = 1024 * 1024   # notes can be long


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fund = read_json(FUNDAMENTALS_PATH)
        self.prices = read_json(PRICES_PATH)
        self.payloads: dict[str, bytes] = {}   # universe id -> JSON bytes
        self.index = b""
        self.health = {"startedAt": iso(utc_now()), "fundamentals": {}, "prices": {}}
        for uid in UNIVERSES:
            data = read_json(universe_path(uid))
            if data:
                self.payloads[uid] = json.dumps(data, separators=(",", ":")).encode()
        idx = read_json(INDEX_PATH)
        if idx:
            self.index = json.dumps(idx).encode()

    def rebuild(self) -> None:
        if not self.fund:
            return
        outs = build.build(fund=self.fund, prices=self.prices)
        idx = read_json(INDEX_PATH)
        with self.lock:
            self.payloads = {u: json.dumps(o, separators=(",", ":")).encode() for u, o in outs.items()}
            self.index = json.dumps(idx).encode() if idx else b""

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
    def progress(out: dict) -> None:
        STATE.fund = out
        STATE.rebuild()

    fails = 0
    while True:
        try:
            todo = len(fundamentals.due(STATE.fund, FUND_INTERVAL))
        except Exception:  # noqa: BLE001  (stock lists unavailable: try the update, it reports why)
            todo = 1
        if not todo:
            time.sleep(60)
            continue
        t0 = time.time()
        try:
            STATE.fund = fundamentals.update(max_age=FUND_INTERVAL, on_progress=progress)
            STATE.rebuild()
            STATE.record("fundamentals", True, time.time() - t0)
            fails = 0
            if STATE.fund.get("pending"):
                time.sleep(300)   # some failed: try those again in a while
        except Exception as e:  # noqa: BLE001
            if isinstance(e, RuntimeError):
                print(f"Fundamentals update skipped: {e}", flush=True)   # expected situation, no traceback
            else:
                traceback.print_exc()
            STATE.record("fundamentals", False, time.time() - t0, f"{type(e).__name__}: {e}")
            fails += 1
            time.sleep(min(300 * 2 ** (fails - 1), 3600))   # 5, 10, 20 ... at most 60 min


def prices_loop() -> None:
    at_start = True   # fetch right away when the server starts, then on the interval
    while True:
        if not at_start:
            time.sleep(PRICE_INTERVAL - (time.time() % PRICE_INTERVAL))   # wake on the interval boundary
        if not STATE.fund:
            time.sleep(10)   # first run ever: wait for the first fundamentals, then fetch at once
            continue
        at_start = False
        if not (PRICES_ALWAYS or market_session(extended=True) or STATE.prices is None):
            continue
        if GATE.paused_for():
            continue   # Yahoo asked us to slow down; skip this round
        t0 = time.time()
        try:
            STATE.prices = prices.update(STATE.fund.get("order"))
            STATE.rebuild()
            STATE.record("prices", True, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, RuntimeError):
                print(f"Price update skipped: {e}", flush=True)
            else:
                traceback.print_exc()
            STATE.record("prices", False, time.time() - t0, f"{type(e).__name__}: {e}")


STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/data/screens.json": ("data/screens.json", "application/json"),
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
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _analysis(self, ticker: str, refresh: bool) -> None:
        from urllib.parse import unquote
        try:
            return self._json(200, analysis.get(unquote(ticker), refresh=refresh))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except LookupError as e:
            return self._json(404, {"error": str(e)})
        except analysis.RateLimited:
            wait = max(1, round(GATE.paused_for() / 60))
            return self._json(503, {"error": f"Yahoo is limiting requests right now. Try again in about {wait} min."})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json(502, {"error": f"Couldn't get the statements from Yahoo Finance ({type(e).__name__})."})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        uid = None
        if path == "/api/sp500":
            uid = "sp500"
        elif path.startswith("/api/data/"):
            uid = path[len("/api/data/"):]
        elif path.startswith("/data/") and path.endswith(".json") and path[6:-5] in UNIVERSES:
            uid = path[6:-5]
        if uid is not None:
            if uid not in UNIVERSES:
                return self._json(404, {"error": f"Unknown universe {uid!r}", "universes": list(UNIVERSES)})
            with STATE.lock:
                body = STATE.payloads.get(uid)
            if not body:
                return self._send(503, b'{"error":"No data yet, first update still running"}', "application/json")
            return self._send(200, body, "application/json")
        if path in ("/api/index", "/data/index.json"):
            with STATE.lock:
                body = STATE.index
            return self._send(200, body, "application/json") if body else self._json(503, {"error": "No data yet"})
        if path == "/api/notes":
            return self._json(200, {"notes": notes_store.list_notes(), "tokenRequired": bool(SCREENS_TOKEN)})
        if path == "/api/storage":
            return self._json(200, storage_info())
        if path.startswith("/api/analysis/"):
            return self._analysis(path[len("/api/analysis/"):], "refresh=1" in self.path.partition("?")[2])
        if path == "/api/watchlists":
            return self._json(200, {"watchlists": screens_db.list_watchlists(), "tokenRequired": bool(SCREENS_TOKEN)})
        if path == "/api/screens":
            return self._json(200, {"screens": screens_db.list_screens(), "tokenRequired": bool(SCREENS_TOKEN)})
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

    # ---- saved screens (SQLite) ----
    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _target(self) -> tuple[str, str] | None:
        """("screens" | "watchlists" | "notes", id) for /api/<kind>/<id>."""
        path = self.path.split("?", 1)[0]
        for kind in ("screens", "watchlists", "notes"):
            prefix = f"/api/{kind}/"
            if path.startswith(prefix):
                return kind, path[len(prefix):]
        return None

    def _authorized(self) -> bool:
        return not SCREENS_TOKEN or self.headers.get("Authorization") == f"Bearer {SCREENS_TOKEN}"

    def do_PUT(self) -> None:  # noqa: N802
        target = self._target()
        if target is None:
            return self._json(404, {"error": "Not found"})
        if not self._authorized():
            return self._json(401, {"error": "Missing or wrong token"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(413 if length else 400, {"error": "Body must be JSON, at most 1 MB"})
        try:
            body = json.loads(self.rfile.read(length))
            kind, sid = target
            save = {"screens": screens_db.upsert, "watchlists": screens_db.upsert_watchlist, "notes": notes_store.upsert_note}[kind]
            return self._json(200, save(sid, body))
        except (ValueError, screens_db.InvalidScreen) as e:
            return self._json(400, {"error": str(e)})

    def do_DELETE(self) -> None:  # noqa: N802
        target = self._target()
        if target is None:
            return self._json(404, {"error": "Not found"})
        if not self._authorized():
            return self._json(401, {"error": "Missing or wrong token"})
        kind, sid = target
        remove = {"screens": screens_db.delete, "watchlists": screens_db.delete_watchlist, "notes": notes_store.delete_note}[kind]
        return self._json(200, {"deleted": remove(sid)})

    def log_message(self, fmt, *args) -> None:  # quieter logs: only errors
        if len(args) < 2 or str(args[1])[:1] in "45":
            super().log_message(fmt, *args)


def storage_info() -> dict:
    return {"db": os.path.abspath(screens_db.DB_PATH), "notesDir": os.path.abspath(notes_store.NOTES_DIR),
            "dataDir": os.path.abspath(os.path.dirname(screens_db.DB_PATH))}


def main() -> None:
    moved = notes_store.migrate_from_sqlite(screens_db.DB_PATH)
    if moved:
        print(f"Moved {moved} notes from the SQLite database into {notes_store.NOTES_DIR}", flush=True)
    threading.Thread(target=fundamentals_loop, name="fundamentals", daemon=True).start()
    threading.Thread(target=prices_loop, name="prices", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    shown = "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST
    print(f"Screener server on http://{shown}:{PORT}  (prices every {PRICE_INTERVAL}s while the "
          f"market is open, fundamentals every {FUND_INTERVAL}s)\n"
          f"  screens + watchlists: {storage_info()['db']}\n  notes: {storage_info()['notesDir']}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
