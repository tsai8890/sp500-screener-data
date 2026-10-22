"""Shared helpers: file locations, JSON I/O, number cleaning and US market hours."""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SCREENER_DATA_DIR", os.path.join(ROOT, "data"))
FUNDAMENTALS_PATH = os.path.join(DATA_DIR, "fundamentals.json")
PRICES_PATH = os.path.join(DATA_DIR, "prices.json")
SP500_PATH = os.path.join(DATA_DIR, "sp500.json")

NY = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def market_session(now: datetime | None = None, grace_minutes: int = 15) -> bool:
    """True on weekdays from 9:30 to 16:00 New York time, plus a short grace
    period after the close so the closing price is captured.
    Exchange holidays are not modelled; on those days prices simply don't change."""
    now = (now or utc_now()).astimezone(NY)
    if now.weekday() >= 5:
        return False
    open_dt = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
    close_dt = now.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0)
    return open_dt <= now <= close_dt + timedelta(minutes=grace_minutes)


def num(v, scale: float = 1.0, nd: int | None = 2):
    """Float or None; drops NaN/inf and non-numeric values."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    f *= scale
    return round(f, nd) if nd is not None else f


def read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: str, obj) -> None:
    """Atomic write so a reader (or the web server) never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)
