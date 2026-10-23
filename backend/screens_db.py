"""Saved screens (filters) and watchlists in SQLite.

Used by the always-on server (backend/server.py) for /api/screens and
/api/watchlists. The database file is data/screener.db (override with SCREENER_DB).

Tables:
  screens(id TEXT PRIMARY KEY, name TEXT, rules TEXT (JSON), sectors TEXT (JSON),
          created INTEGER (ms), updated INTEGER (ms))
  watchlists(id TEXT PRIMARY KEY, name TEXT, items TEXT (JSON: [{ticker, addedAt, addedPrice}]),
             created INTEGER (ms), updated INTEGER (ms))
  notes(id TEXT PRIMARY KEY, body TEXT (Markdown), pinned INTEGER (0/1),
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
    conn.execute("""CREATE TABLE IF NOT EXISTS notes (
        id TEXT PRIMARY KEY, body TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
        created INTEGER NOT NULL, updated INTEGER NOT NULL)""")
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


# ---- notes (Markdown) --------------------------------------------------------

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


def list_notes(path: str = DB_PATH) -> list[dict]:
    with _lock, _connect(path) as conn:
        rows = conn.execute("SELECT id, body, pinned, created, updated FROM notes ORDER BY pinned DESC, updated DESC").fetchall()
    return [{"id": r[0], "body": r[1], "pinned": bool(r[2]), "created": r[3], "updated": r[4]} for r in rows]


def upsert_note(note_id: str, body: dict, path: str = DB_PATH) -> dict:
    """Save a note. The client's own `updated` time is kept (so a note edited
    offline keeps its real edit time), but an older copy never overwrites a newer one."""
    n = validate_note(note_id, body)
    now = int(time.time() * 1000)
    updated = n["updated"] or now
    with _lock, _connect(path) as conn:
        row = conn.execute("SELECT created, updated FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row and row[1] > updated:   # stale write: keep what is stored
            cur = conn.execute("SELECT id, body, pinned, created, updated FROM notes WHERE id = ?", (note_id,)).fetchone()
            return {"id": cur[0], "body": cur[1], "pinned": bool(cur[2]), "created": cur[3], "updated": cur[4], "stale": True}
        if not row and conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] >= MAX_NOTES:
            raise InvalidScreen(f"at most {MAX_NOTES} notes")
        created = row[0] if row else (n["created"] or now)
        conn.execute("""INSERT INTO notes (id, body, pinned, created, updated) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET body=excluded.body, pinned=excluded.pinned, updated=excluded.updated""",
                     (note_id, n["body"], int(n["pinned"]), created, updated))
    return {**n, "created": created, "updated": updated}


def delete_note(note_id: str, path: str = DB_PATH) -> bool:
    with _lock, _connect(path) as conn:
        return conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0
