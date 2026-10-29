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
UNIVERSES_PATH = os.path.join(DATA_DIR, "universes.json")   # cached index constituents
INDEX_PATH = os.path.join(DATA_DIR, "index.json")           # list of universes for the page


SEC_FUNDAMENTALS_PATH = os.path.join(DATA_DIR, "sec_fundamentals.json")   # written by sec_sync.py


def universe_path(uid: str) -> str:
    """Combined data file the page reads for one universe, e.g. data/russell1000.json."""
    return os.path.join(DATA_DIR, f"{uid}.json")

NY = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
PRE_OPEN = dtime(4, 0)       # pre-market trading starts
POST_CLOSE = dtime(20, 0)    # after-hours trading ends


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


def market_session(now: datetime | None = None, grace_minutes: int = 15, extended: bool = False) -> bool:
    """True on weekdays from 9:30 to 16:00 New York time (4:00 to 20:00 with
    extended=True: pre-market and after-hours), plus a short grace period after
    the close so the last price is captured.
    Exchange holidays are not modelled; on those days prices simply don't change."""
    now = (now or utc_now()).astimezone(NY)
    if now.weekday() >= 5:
        return False
    start, end = (PRE_OPEN, POST_CLOSE) if extended else (MARKET_OPEN, MARKET_CLOSE)
    open_dt = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    close_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
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


def load_dotenv(path: str) -> None:
    """KEY=value lines from a .env file into the environment (existing variables win).
    API keys and contact details live there, git-ignored, never in the code."""
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
