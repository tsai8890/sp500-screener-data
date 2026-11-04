"""Saved screens (filters) and watchlists in SQLite, on this machine.

Used by the local server (backend/server.py) for /api/screens and
/api/watchlists. The database file is data/screener.db (override with SCREENER_DB).
Notes are not here: they are Markdown files in data/notes/ (backend/notes_store.py).

Tables:
  screens(id TEXT PRIMARY KEY, name TEXT, rules TEXT (JSON), sectors TEXT (JSON),
          created INTEGER (ms), updated INTEGER (ms))
  watchlists(id TEXT PRIMARY KEY, name TEXT, items TEXT (JSON: [{ticker, addedAt, addedPrice}]),
             created INTEGER (ms), updated INTEGER (ms))

Inspect with:  sqlite3 data/screener.db "select id, name, items from watchlists"
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time

from .common import DATA_DIR

DB_PATH = os.environ.get("SCREENER_DB", os.path.join(DATA_DIR, "screener.db"))

FIELDS = {"price", "chg", "mktCap", "pe", "fpe", "peg", "revGrowth", "netMargin", "divYield"}
OPS = {">", ">=", "<", "<=", "between"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_SCREENS = 500

_lock = threading.Lock()


class InvalidScreen(ValueError):
    pass


def _connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS screens (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, rules TEXT NOT NULL, sectors TEXT NOT NULL,
        created INTEGER NOT NULL, updated INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS watchlists (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, items TEXT NOT NULL,
        created INTEGER NOT NULL, updated INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY, date TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
        shares REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
        created INTEGER NOT NULL, updated INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fund_snapshots (
        id TEXT PRIMARY KEY, fund TEXT NOT NULL, account TEXT NOT NULL, date TEXT NOT NULL,
        units REAL NOT NULL, nav REAL NOT NULL, proxy TEXT NOT NULL DEFAULT '', proxy_close REAL,
        cost REAL, created INTEGER NOT NULL, updated INTEGER NOT NULL)""")
    if "kind" not in [r[1] for r in conn.execute("PRAGMA table_info(fund_snapshots)")]:
        conn.execute("ALTER TABLE fund_snapshots ADD COLUMN kind TEXT NOT NULL DEFAULT 'fund'")
    return conn


def validate(screen_id: str, body: dict) -> dict:
    """Return a clean screen dict or raise InvalidScreen."""
    if not ID_RE.match(screen_id or ""):
        raise InvalidScreen("id must be 1-64 letters, digits, - or _")
    if not isinstance(body, dict):
        raise InvalidScreen("body must be a JSON object")
    name = str(body.get("name") or "").strip()[:100] or "Untitled screen"
    rules_in = body.get("rules") or []
    if not isinstance(rules_in, list) or len(rules_in) > 20:
        raise InvalidScreen("rules must be a list of at most 20 rules")
    rules = []
    for r in rules_in:
        if not isinstance(r, dict) or r.get("field") not in FIELDS or r.get("op") not in OPS:
            raise InvalidScreen(f"bad rule: {r!r}")
        rule = {"field": r["field"], "op": r["op"], "value": _number(r.get("value"))}
        if r["op"] == "between":
            rule["value2"] = _number(r.get("value2"))
        rules.append(rule)
    sectors = body.get("sectors") or []
    if not isinstance(sectors, list) or len(sectors) > 20 or not all(isinstance(s, str) for s in sectors):
        raise InvalidScreen("sectors must be a list of at most 20 names")
    created = body.get("created")
    return {"id": screen_id, "name": name, "rules": rules, "sectors": [s[:60] for s in sectors],
            "created": int(created) if isinstance(created, (int, float)) else None}


def _number(v):
    if v == "" or v is None:
        return ""
    try:
        return float(v) if not float(v).is_integer() else int(float(v))
    except (TypeError, ValueError):
        raise InvalidScreen(f"not a number: {v!r}") from None


def _row(r) -> dict:
    return {"id": r[0], "name": r[1], "rules": json.loads(r[2]), "sectors": json.loads(r[3]),
            "created": r[4], "updated": r[5]}


def list_screens(path: str = DB_PATH) -> list[dict]:
    with _lock, _connect(path) as conn:
        rows = conn.execute("SELECT id, name, rules, sectors, created, updated FROM screens ORDER BY created").fetchall()
    return [_row(r) for r in rows]


def upsert(screen_id: str, body: dict, path: str = DB_PATH) -> dict:
    s = validate(screen_id, body)
    now = int(time.time() * 1000)
    with _lock, _connect(path) as conn:
        exists = conn.execute("SELECT created FROM screens WHERE id = ?", (screen_id,)).fetchone()
        if not exists and conn.execute("SELECT COUNT(*) FROM screens").fetchone()[0] >= MAX_SCREENS:
            raise InvalidScreen(f"at most {MAX_SCREENS} screens")
        created = exists[0] if exists else (s["created"] or now)
        conn.execute("""INSERT INTO screens (id, name, rules, sectors, created, updated) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name, rules=excluded.rules,
                        sectors=excluded.sectors, updated=excluded.updated""",
                     (screen_id, s["name"], json.dumps(s["rules"]), json.dumps(s["sectors"]), created, now))
    return {**s, "created": created, "updated": now}


def delete(screen_id: str, path: str = DB_PATH) -> bool:
    with _lock, _connect(path) as conn:
        return conn.execute("DELETE FROM screens WHERE id = ?", (screen_id,)).rowcount > 0


# ---- watchlists ------------------------------------------------------------

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
MAX_ITEMS = 500


def validate_watchlist(wl_id: str, body: dict) -> dict:
    if not ID_RE.match(wl_id or ""):
        raise InvalidScreen("id must be 1-64 letters, digits, - or _")
    if not isinstance(body, dict):
        raise InvalidScreen("body must be a JSON object")
    name = str(body.get("name") or "").strip()[:100] or "Watchlist"
    items_in = body.get("items") or []
    if not isinstance(items_in, list) or len(items_in) > MAX_ITEMS:
        raise InvalidScreen(f"items must be a list of at most {MAX_ITEMS} stocks")
    items, seen = [], set()
    for it in items_in:
        t = str((it or {}).get("ticker") or "").strip().upper() if isinstance(it, dict) else ""
        if not TICKER_RE.match(t):
            raise InvalidScreen(f"bad ticker: {it!r}")
        if t in seen:
            continue
        seen.add(t)
        added_at = it.get("addedAt")
        price = it.get("addedPrice")
        items.append({"ticker": t,
                      "addedAt": int(added_at) if isinstance(added_at, (int, float)) else None,
                      "addedPrice": float(price) if isinstance(price, (int, float)) and price > 0 else None})
    created = body.get("created")
    return {"id": wl_id, "name": name, "items": items,
            "created": int(created) if isinstance(created, (int, float)) else None}


def list_watchlists(path: str = DB_PATH) -> list[dict]:
    with _lock, _connect(path) as conn:
        rows = conn.execute("SELECT id, name, items, created, updated FROM watchlists ORDER BY created").fetchall()
    return [{"id": r[0], "name": r[1], "items": json.loads(r[2]), "created": r[3], "updated": r[4]} for r in rows]


def upsert_watchlist(wl_id: str, body: dict, path: str = DB_PATH) -> dict:
    w = validate_watchlist(wl_id, body)
    now = int(time.time() * 1000)
    with _lock, _connect(path) as conn:
        exists = conn.execute("SELECT created FROM watchlists WHERE id = ?", (wl_id,)).fetchone()
        if not exists and conn.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0] >= MAX_SCREENS:
            raise InvalidScreen(f"at most {MAX_SCREENS} watchlists")
        created = exists[0] if exists else (w["created"] or now)
        conn.execute("""INSERT INTO watchlists (id, name, items, created, updated) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name, items=excluded.items,
                        updated=excluded.updated""",
                     (wl_id, w["name"], json.dumps(w["items"]), created, now))
    return {**w, "created": created, "updated": now}


def delete_watchlist(wl_id: str, path: str = DB_PATH) -> bool:
    with _lock, _connect(path) as conn:
        return conn.execute("DELETE FROM watchlists WHERE id = ?", (wl_id,)).rowcount > 0


# ---- portfolio transactions -------------------------------------------------------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TRANSACTIONS = 20000


def validate_transaction(tx_id: str, body: dict) -> dict:
    """One buy or sell: {date: YYYY-MM-DD, ticker, side: buy|sell, shares > 0, price >= 0, fee >= 0, note}."""
    if not ID_RE.match(tx_id or ""):
        raise InvalidScreen("id must be 1-64 letters, digits, - or _")
    if not isinstance(body, dict):
        raise InvalidScreen("body must be a JSON object")
    date = str(body.get("date") or "")
    if not DATE_RE.match(date):
        raise InvalidScreen("date must be YYYY-MM-DD")
    ticker = str(body.get("ticker") or "").strip().upper().replace("-", ".")
    if not TICKER_RE.match(ticker):
        raise InvalidScreen(f"bad ticker: {body.get('ticker')!r}")
    side = str(body.get("side") or "").lower()
    if side not in ("buy", "sell"):
        raise InvalidScreen("side must be buy or sell")
    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    shares, price, fee = num(body.get("shares")), num(body.get("price")), num(body.get("fee") or 0)
    if shares is None or shares <= 0:
        raise InvalidScreen("shares must be a number above 0")
    if price is None or price < 0:
        raise InvalidScreen("price must be a number, 0 or more")
    if fee is None or fee < 0:
        raise InvalidScreen("fee must be a number, 0 or more")
    created = body.get("created")
    return {"id": tx_id, "date": date, "ticker": ticker, "side": side, "shares": shares, "price": price,
            "fee": fee, "note": str(body.get("note") or "")[:200],
            "created": int(created) if isinstance(created, (int, float)) else None}


def list_transactions(path: str = DB_PATH) -> list[dict]:
    with _lock, _connect(path) as conn:
        rows = conn.execute("""SELECT id, date, ticker, side, shares, price, fee, note, created, updated
                               FROM transactions ORDER BY date, created""").fetchall()
    keys = ("id", "date", "ticker", "side", "shares", "price", "fee", "note", "created", "updated")
    return [dict(zip(keys, r)) for r in rows]


def upsert_transaction(tx_id: str, body: dict, path: str = DB_PATH) -> dict:
    t = validate_transaction(tx_id, body)
    now = int(time.time() * 1000)
    with _lock, _connect(path) as conn:
        exists = conn.execute("SELECT created FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not exists and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] >= MAX_TRANSACTIONS:
            raise InvalidScreen(f"at most {MAX_TRANSACTIONS} transactions")
        created = exists[0] if exists else (t["created"] or now)
        conn.execute("""INSERT INTO transactions (id, date, ticker, side, shares, price, fee, note, created, updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET date=excluded.date, ticker=excluded.ticker, side=excluded.side,
                        shares=excluded.shares, price=excluded.price, fee=excluded.fee, note=excluded.note,
                        updated=excluded.updated""",
                     (tx_id, t["date"], t["ticker"], t["side"], t["shares"], t["price"], t["fee"], t["note"], created, now))
    return {**t, "created": created, "updated": now}


def delete_transaction(tx_id: str, path: str = DB_PATH) -> bool:
    with _lock, _connect(path) as conn:
        return conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,)).rowcount > 0


def portfolio_tickers(path: str = DB_PATH) -> list[str]:
    """Every ticker in a transaction, and every fund's proxy (their prices are kept up to date too)."""
    with _lock, _connect(path) as conn:
        rows = conn.execute("""SELECT ticker FROM transactions UNION SELECT proxy FROM fund_snapshots
                               WHERE proxy != '' ORDER BY 1""").fetchall()
    return [r[0] for r in rows]


# ---- assets without a ticker: funds (401(k) trusts ...) and cash ------------------
# Each update is one snapshot, kept as history; the latest per name counts (0 = removed).
#   fund  units and NAV on a date. Between updates the value is estimated from a tracking
#         ETF (the proxy, e.g. VOO for an S&P 500 trust):
#         NAV today ~ snapshot NAV x proxy price now / proxy close on the snapshot date.
#   cash  the balance: units = amount, nav = 1.

def validate_fund_snapshot(snap_id: str, body: dict) -> dict:
    if not ID_RE.match(snap_id or ""):
        raise InvalidScreen("id must be 1-64 letters, digits, - or _")
    if not isinstance(body, dict):
        raise InvalidScreen("body must be a JSON object")

    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    kind = str(body.get("kind") or "fund")
    if kind not in ("fund", "cash"):
        raise InvalidScreen("kind must be fund or cash")
    if kind == "cash":
        body = {**body, "nav": 1, "proxy": "", "proxyClose": None, "cost": None}
    fund = str(body.get("fund") or "").strip()[:100]
    if not fund:
        raise InvalidScreen("a name is required")
    date = str(body.get("date") or "")
    if not DATE_RE.match(date):
        raise InvalidScreen("date must be YYYY-MM-DD")
    units, nav = num(body.get("units")), num(body.get("nav"))
    if units is None or units < 0:
        raise InvalidScreen(("the amount" if kind == "cash" else "units") + " must be a number, 0 or more")
    if nav is None or nav <= 0:
        raise InvalidScreen("NAV must be a number above 0")
    proxy = str(body.get("proxy") or "").strip().upper()
    if proxy and not TICKER_RE.match(proxy):
        raise InvalidScreen(f"bad proxy ticker: {proxy!r}")
    cost = num(body.get("cost")) if body.get("cost") not in (None, "") else None
    if cost is not None and cost < 0:
        raise InvalidScreen("total contributed must be 0 or more")
    pc = num(body.get("proxyClose")) if body.get("proxyClose") not in (None, "") else None
    created = body.get("created")
    return {"id": snap_id, "kind": kind, "fund": fund, "account": str(body.get("account") or "").strip()[:40],
            "date": date, "units": units, "nav": nav, "proxy": proxy, "proxyClose": pc if pc and pc > 0 else None,
            "cost": cost, "created": int(created) if isinstance(created, (int, float)) else None}


_SNAP_COLS = ("id", "kind", "fund", "account", "date", "units", "nav", "proxy", "proxyClose", "cost", "created", "updated")


def list_fund_snapshots(path: str = DB_PATH) -> list[dict]:
    with _lock, _connect(path) as conn:
        rows = conn.execute("""SELECT id, kind, fund, account, date, units, nav, proxy, proxy_close, cost, created, updated
                               FROM fund_snapshots ORDER BY date, created""").fetchall()
    return [dict(zip(_SNAP_COLS, r)) for r in rows]


def upsert_fund_snapshot(snap_id: str, body: dict, path: str = DB_PATH) -> dict:
    f = validate_fund_snapshot(snap_id, body)
    now = int(time.time() * 1000)
    with _lock, _connect(path) as conn:
        exists = conn.execute("SELECT created FROM fund_snapshots WHERE id = ?", (snap_id,)).fetchone()
        created = exists[0] if exists else (f["created"] or now)
        conn.execute("""INSERT INTO fund_snapshots (id, kind, fund, account, date, units, nav, proxy, proxy_close, cost, created, updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, fund=excluded.fund, account=excluded.account, date=excluded.date,
                        units=excluded.units, nav=excluded.nav, proxy=excluded.proxy, proxy_close=excluded.proxy_close,
                        cost=excluded.cost, updated=excluded.updated""",
                     (snap_id, f["kind"], f["fund"], f["account"], f["date"], f["units"], f["nav"], f["proxy"], f["proxyClose"],
                      f["cost"], created, now))
    return {**f, "created": created, "updated": now}


def delete_fund_snapshot(snap_id: str, path: str = DB_PATH) -> bool:
    with _lock, _connect(path) as conn:
        return conn.execute("DELETE FROM fund_snapshots WHERE id = ?", (snap_id,)).rowcount > 0


# ---- notes: validation only; storage is backend/notes_store.py ------------------

MAX_NOTE_CHARS = 200_000
MAX_NOTES = 2000


def validate_note(note_id: str, body: dict) -> dict:
    if not ID_RE.match(note_id or ""):
        raise InvalidScreen("id must be 1-64 letters, digits, - or _")
    if not isinstance(body, dict):
        raise InvalidScreen("body must be a JSON object")
    text = body.get("body", "")
    if not isinstance(text, str):
        raise InvalidScreen("body must be Markdown text")
    if len(text) > MAX_NOTE_CHARS:
        raise InvalidScreen(f"a note can hold at most {MAX_NOTE_CHARS:,} characters")
    num = lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"id": note_id, "body": text, "pinned": bool(body.get("pinned")),
            "created": num(body.get("created")), "updated": num(body.get("updated"))}
