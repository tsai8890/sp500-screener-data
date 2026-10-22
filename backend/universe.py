"""S&P 500 constituents with GICS sectors."""
from __future__ import annotations

import io
import sys

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (sp500-screener data job)"}
WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CSV_FALLBACK = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def constituents() -> list[tuple[str, str, str]]:
    """[(ticker, name, sector), ...] in index order."""
    try:
        html = requests.get(WIKI, headers=UA, timeout=30).text
        df = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    except Exception as e:  # noqa: BLE001
        print(f"Wikipedia failed ({e}); using GitHub datasets CSV", file=sys.stderr)
        df = pd.read_csv(io.StringIO(requests.get(CSV_FALLBACK, headers=UA, timeout=30).text))
    df = df.rename(columns={"Symbol": "ticker", "Security": "name", "GICS Sector": "sector"})
    df = df[["ticker", "name", "sector"]].dropna(subset=["ticker"])
    out = [(str(t).strip(), str(n).strip(), str(s).strip()) for t, n, s in df.itertuples(index=False)]
    if len(out) < 450:
        raise RuntimeError(f"Constituent list looks wrong: {len(out)} rows")
    return out


def yahoo_symbol(ticker: str) -> str:
    """Yahoo uses dashes for share classes: BRK.B -> BRK-B."""
    return ticker.replace(".", "-")
