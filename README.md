# S&P 500 Screener

A stock screener for the S&P 500. You build screens from rules on P/E, PEG, revenue growth, net margin, dividend yield and market cap, save them, and switch between them to compare results. A Python job refreshes the data every 3 hours.

Live site: https://tsai8890.github.io/sp500-screener-data/

## How it fits together

```
GitHub Actions (every 3 h)
  └─ backend/fetch_sp500.py  ──writes──▶  data/sp500.json  (the "database", committed to the repo)
                                                │
GitHub Pages serves the repo root               │
  └─ index.html  ──fetch("data/sp500.json")─────┘
```

| Path | What it is |
|---|---|
| `index.html` | The whole frontend: plain HTML, CSS and JavaScript, no framework or build step. |
| `data/sp500.json` | The dataset. Rewritten by the backend on every run. |
| `backend/fetch_sp500.py` | Python backend. Gets the S&P 500 list from Wikipedia and per-stock metrics from Yahoo Finance via `yfinance`. |
| `backend/requirements.txt` | Python dependencies. |
| `.github/workflows/update-data.yml` | Runs the backend every 3 hours and commits the new data. |

Saved screens are kept in each visitor's browser (localStorage).

## Data format

`data/sp500.json` stores rows as arrays to stay small (about 36 KB):

```json
{
  "asOf": "2026-10-22T16:22:08Z",
  "source": "Yahoo Finance via yfinance; constituents from Wikipedia",
  "count": 503,
  "stale": [],
  "cols": ["ticker", "name", "sector", "mktCap", "pe", "peg", "revGrowth", "netMargin", "divYield"],
  "rows": [["AAPL", "Apple Inc.", "Information Technology", 5046.8, 39.7, 2.71, 16.4, 27.6, 0.31], ...]
}
```

Units: `mktCap` in billions of USD; `revGrowth`, `netMargin`, `divYield` in percent. `pe` is null for negative earnings. `revGrowth` is the latest quarter compared with the same quarter a year earlier, and `peg` is Yahoo's trailing PEG.

## Run locally

```bash
pip install -r backend/requirements.txt
python backend/fetch_sp500.py          # refreshes data/sp500.json
python -m http.server 8000             # then open http://localhost:8000
```

Opening `index.html` straight from disk (`file://`) shows the built-in example data, because browsers block `fetch` there. Serve the folder instead.

## Notes

- Yahoo Finance data through `yfinance` is free but unofficial and can be rate-limited or change without notice. If a stock fails to download, its previous row is kept and listed in `stale`. If fewer than 80% of stocks download fresh, the run fails and the previous file stays in place.
- The schedule can start a few minutes late when GitHub is busy.
