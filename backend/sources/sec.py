"""SEC EDGAR: the official source of what companies file (10-Q, 10-K).

Everything here is free and public, with two rules from the SEC:
  * every request names who is asking: SEC_USER_AGENT, e.g. "Jane Doe jane@example.com"
    (put it in .env; without it this module is switched off and Yahoo is used alone)
  * at most 10 requests a second; going over gets this computer blocked for ~10 minutes.
    SEC_RPS (default 4) paces background work; a page waiting on a stock may go faster.

Endpoints used:
  https://www.sec.gov/files/company_tickers.json          ticker -> CIK (company id), cached a day
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   every reported number (XBRL)
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent ...  the newest filings (Atom feed)
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

import requests

from ..common import ROOT, iso, parse_iso, read_json, utc_now, write_json

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKERS_PATH = os.path.join(ROOT, "data", "sec", "company_tickers.json")
RPS = float(os.environ.get("SEC_RPS", "4"))
BLOCK_PAUSE = 11 * 60   # the SEC blocks over-eager clients for about 10 minutes


class Unavailable(Exception):
    """SEC is switched off (no SEC_USER_AGENT), blocking us, or unreachable."""


def user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", "").strip()


def enabled() -> bool:
    ua = user_agent()
    return "@" in ua   # the SEC asks for a contact email


class _Pace:
    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / max(rps, 0.1)
        self.lock = threading.Lock()
        self.next = 0.0
        self.blocked_until = 0.0

    def wait(self, priority: bool = False) -> None:
        if time.monotonic() < self.blocked_until:
            raise Unavailable("SEC asked us to slow down; trying again in a few minutes")
        step = 0.12 if priority else self.interval   # priority still stays under 10/s
        while True:
            with self.lock:
                now = time.monotonic()
                if now >= self.next:
                    self.next = now + step
                    return
                delay = self.next - now
            time.sleep(delay)


PACE = _Pace(RPS)
_session = requests.Session() if hasattr(requests, "Session") else None


def _get(url: str, params: dict | None = None, priority: bool = False, timeout: int = 30):
    if not enabled():
        raise Unavailable("SEC_USER_AGENT is not set (name and email, in .env)")
    PACE.wait(priority)
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    getter = _session.get if _session else requests.get
    try:
        r = getter(url, params=params, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise Unavailable(f"can't reach the SEC ({type(e).__name__})") from e
    if r.status_code in (403, 429):
        PACE.blocked_until = time.monotonic() + BLOCK_PAUSE
        from .. import console
        console.log(f"SEC: request refused ({r.status_code}); pausing SEC requests for {BLOCK_PAUSE // 60} min.", error=True)
        raise Unavailable("SEC is limiting requests right now")
    if r.status_code == 404:
        raise LookupError(url)
    r.raise_for_status()
    return r


# ---------- ticker -> CIK ----------

_cik_lock = threading.Lock()
_cik_map: dict[str, int] | None = None


def _norm(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")   # the SEC writes BRK-B, the lists BRK.B


def cik_for(ticker: str) -> int | None:
    """The company's CIK, or None if the SEC doesn't list the ticker."""
    global _cik_map
    with _cik_lock:
        if _cik_map is None:
            cached = read_json(TICKERS_PATH)
            fresh = cached and parse_iso(cached.get("asOf")) and \
                (utc_now() - parse_iso(cached["asOf"])).total_seconds() < 86400
            if not fresh:
                try:
                    data = _get(TICKERS_URL, priority=True).json()
                    cached = {"asOf": iso(utc_now()),
                              "map": {_norm(v["ticker"]): int(v["cik_str"]) for v in data.values()}}
                    os.makedirs(os.path.dirname(TICKERS_PATH), exist_ok=True)
                    write_json(TICKERS_PATH, cached)
                except Unavailable:
                    if not cached:
                        raise
            _cik_map = (cached or {}).get("map", {})
        return _cik_map.get(_norm(ticker))


def ciks() -> dict[str, int]:
    """{ticker (SEC spelling): CIK} for every company the SEC lists."""
    cik_for("AAPL")   # loads the map
    return dict(_cik_map or {})


# ---------- company facts ----------

def company_facts(cik: int, priority: bool = False) -> dict:
    """Every number the company has reported in XBRL, as the SEC publishes it."""
    return _get(FACTS_URL.format(cik=cik), priority=priority, timeout=60).json()


# ---------- newest filings ----------

_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_UPDATED = re.compile(r"<updated>(.*?)</updated>", re.S)
_CIK = re.compile(r"\((\d{10})\)")


def latest_filings(form: str, count: int = 100) -> list[dict]:
    """The newest filings of one form type: [{"cik", "form", "company", "updated"}], newest first.
    The SEC updates this feed within minutes of a filing being accepted."""
    params = {"action": "getcurrent", "type": form, "company": "", "dateb": "", "owner": "include",
              "start": 0, "count": count, "output": "atom"}
    text = _get(FEED_URL, params=params).text
    out = []
    for entry in _ENTRY.findall(text):
        t = _TITLE.search(entry)
        title = t.group(1) if t else ""
        m = _CIK.search(title)
        if not m:
            continue
        upd = _UPDATED.search(entry)
        out.append({"cik": int(m.group(1)), "form": title.split(" - ", 1)[0].strip(),
                    "company": re.sub(r"\s*\(\d{10}\).*$", "", title.split(" - ", 1)[-1]).strip(),
                    "updated": upd.group(1).strip() if upd else None})
    return out
