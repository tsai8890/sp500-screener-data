"""Stock universes (index constituents) the screener can show.

Each universe is a list of (ticker, name, sector). Lists are cached in
data/universes.json and refreshed at most once a day, because index
membership changes rarely. Fundamentals and prices are fetched once for the
union of all universes, then build.py writes one data/<id>.json per universe.

To add another universe, write a function returning [(ticker, name, sector)]
and register it in UNIVERSES.
"""
from __future__ import annotations

import io
import sys
from datetime import timedelta

import pandas as pd
import requests

from .common import UNIVERSES_PATH, iso, parse_iso, read_json, utc_now, write_json

UA = {"User-Agent": "Mozilla/5.0 (sp500-screener data job; +https://github.com/tsai8890/sp500-screener-data)"}
MAX_AGE = timedelta(hours=20)


def _get(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


def _clean(df: pd.DataFrame, ticker_col: str, name_col: str | None, sector_col: str | None) -> list[tuple[str, str, str]]:
    out, seen = [], set()
    for _, row in df.iterrows():
        t = str(row[ticker_col]).strip().upper()
        if not t or t == "NAN" or t in seen or len(t) > 8:
            continue
        seen.add(t)
        name = str(row[name_col]).strip() if name_col and pd.notna(row[name_col]) else t
        sector = str(row[sector_col]).strip() if sector_col and pd.notna(row[sector_col]) else "Unclassified"
        out.append((t, name, sector))
    return out


def _find_col(columns, *keys) -> str | None:
    for c in columns:
        lc = str(c).lower()
        if any(k in lc for k in keys):
            return c
    return None


def _largest_wiki_table(url: str, min_rows: int) -> list[tuple[str, str, str]]:
    """The biggest table on a Wikipedia page that has a ticker/symbol column."""
    tables = pd.read_html(io.StringIO(_get(url)))
    best = []
    for df in tables:
        tcol = _find_col(df.columns, "symbol", "ticker")
        if tcol is None or len(df) < min_rows:
            continue
        rows = _clean(df, tcol, _find_col(df.columns, "security", "company", "name"), _find_col(df.columns, "sector"))
        if len(rows) > len(best):
            best = rows
    if len(best) < min_rows:
        raise RuntimeError(f"no constituents table with {min_rows}+ rows at {url}")
    return best


# ---- sources -------------------------------------------------------------

def sp500() -> list[tuple[str, str, str]]:
    try:
        return _largest_wiki_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 450)
    except Exception as e:  # noqa: BLE001
        print(f"S&P 500 from Wikipedia failed ({e}); using GitHub datasets CSV", file=sys.stderr)
        df = pd.read_csv(io.StringIO(_get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv")))
        return _clean(df, "Symbol", "Security", "GICS Sector")


IWB = "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
IWB_FILES = [IWB + "latest-holdings.csv",
             IWB + "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"]
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
              "Accept": "text/csv,application/csv,text/plain,*/*"}


def _ishares_rows(text: str) -> list[tuple[str, str, str]]:
    lines = text.replace("\ufeff", "").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"] == "Equity"]
    rows = []
    for t, n, sec in _clean(df, "Ticker", "Name", "Sector"):
        t = t.replace(" ", ".")                       # "BRK B" -> "BRK.B"
        rows.append((ISHARES_CLASS.get(t, t), n.title(), sec))
    return rows


def russell1000() -> list[tuple[str, str, str]]:
    """Russell 1000 members from the holdings of iShares' Russell 1000 ETF (IWB),
    which tracks the index. Wikipedia has no constituents table for this index."""
    errors = []
    for url in IWB_FILES:
        try:
            r = requests.get(url, headers=BROWSER_UA, timeout=30)
            r.raise_for_status()
            rows = _ishares_rows(r.text)
            if len(rows) >= 900:
                return rows
            errors.append(f"{url}: only {len(rows)} rows")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {e}")
    raise RuntimeError("; ".join(errors))


# iShares writes share-class tickers without the dot (BRKB); Wikipedia and Yahoo use BRK.B / BRK-B.
ISHARES_CLASS = {"BRKB": "BRK.B", "BFB": "BF.B", "BFA": "BF.A", "LENB": "LEN.B", "HEIA": "HEI.A",
                 "UHALB": "UHAL.B", "CWENA": "CWEN.A", "MOGA": "MOG.A", "CRDA": "CRD.A"}

UNIVERSES = {
    "sp500": ("S&P 500", sp500),
    "russell1000": ("Russell 1000", russell1000),
}
DEFAULT = "sp500"


# ---- cache ---------------------------------------------------------------

def load(force: bool = False) -> dict:
    """{"asOf", "universes": {id: {"name", "asOf", "stocks": [[ticker, name, sector], ...]}}}

    Refreshes lists older than MAX_AGE. A list that fails to refresh keeps its
    cached copy; a list that has never loaded is left out (except S&P 500,
    which is required)."""
    cache = read_json(UNIVERSES_PATH) or {"universes": {}}
    lists = cache.get("universes", {})
    errors = {}
    changed = False
    for uid, (name, fetch) in UNIVERSES.items():
        cur = lists.get(uid)
        fresh = cur and parse_iso(cur.get("asOf")) and utc_now() - parse_iso(cur["asOf"]) < MAX_AGE
        if fresh and not force:
            continue
        try:
            rows = fetch()
            lists[uid] = {"name": name, "asOf": iso(utc_now()), "stocks": [list(r) for r in rows]}
            changed = True
            print(f"Universe {name}: {len(rows)} stocks")
        except Exception as e:  # noqa: BLE001
            print(f"Universe {name} refresh failed: {e}", file=sys.stderr)
            errors[uid] = {"at": iso(utc_now()), "error": str(e)[:500]}
            changed = True
            if uid == DEFAULT and not cur:
                raise RuntimeError(f"Could not load the {name} list") from e
    # drop universes that are no longer configured
    for uid in list(lists):
        if uid not in UNIVERSES:
            del lists[uid]
            changed = True
    cache = {"asOf": iso(utc_now()) if changed else cache.get("asOf"), "universes": lists, "errors": errors}
    if changed:
        write_json(UNIVERSES_PATH, cache)
    return cache


def union(cache: dict) -> list[tuple[str, str, str]]:
    """All tickers across universes, default universe first; the first
    universe that lists a ticker supplies its name and sector."""
    order = [DEFAULT] + [u for u in cache["universes"] if u != DEFAULT]
    seen, out = set(), []
    for uid in order:
        for t, name, sector in cache["universes"].get(uid, {}).get("stocks", []):
            if t not in seen:
                seen.add(t)
                out.append((t, name, sector))
    return out


def yahoo_symbol(ticker: str) -> str:
    """Yahoo uses dashes for share classes: BRK.B -> BRK-B."""
    return ticker.replace(".", "-")
