"""Saved screens (filters) in SQLite.

Used by the always-on server (backend/server.py) for the /api/screens endpoints.
The database file is data/screener.db (override with SCREENER_DB).

Table:
  screens(id TEXT PRIMARY KEY, name TEXT, rules TEXT (JSON), sectors TEXT (JSON),
          created INTEGER (ms), updated INTEGER (ms))

Inspect it with:  sqlite3 data/screener.db "select id, name, rules from screens"
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
