# S&P 500 Screener

A stock screener for the S&P 500 and the Russell 1000 (pick the list at the top of the page). You build screens from rules on price, P/E, forward P/E, PEG, revenue growth, net margin, dividend yield and market cap, save them, and switch between them to compare results. Sections are tabs down the left side of the page (a bottom bar on phones); the second keeps your **watchlists**: star any stock (☆) or add it by ticker, and see the same live metrics plus the change since you added it. Open the site with `#watchlist` to land on that tab.

It runs entirely on your own computer: one Python process downloads the market data, serves the page at **http://localhost:8000**, and keeps everything you save in the `data/` folder. Nothing is stored on GitHub or any other service; the only outside connection is to Yahoo Finance (and Wikipedia / iShares for the stock lists).

| | |
|---|---|
| Prices | every 5 minutes during US market hours |
| Fundamentals | each stock every 6 hours |
| Screens, watchlists | SQLite: `data/screener.db` |
| Notes | Markdown files: `data/notes/<id>.md` |

P/E, PEG, market cap and dividend yield depend on the share price, so they are recalculated from the latest price every time prices update. The slower fundamentals (EPS, implied growth, revenue growth, margins, dividends) refresh every 6 hours; they change at most once a quarter.

## Run it

Needs Python 3.10 to 3.14 (pandas has no ready-made packages for other versions yet). `run.sh` picks a suitable one if several are installed; if it says none is, install Python 3.13 with the macOS installer from [python.org](https://www.python.org/downloads/macos/) (or `brew install python@3.13` if you use Homebrew) and try again. To force one: `PYTHON=python3.12 ./run.sh`.

```bash
git clone https://github.com/tsai8890/sp500-screener-data ~/sp500-screener && cd ~/sp500-screener
./run.sh
```

Then open http://localhost:8000. The first run creates `.venv` and installs the packages; the first data download (about 1,000 stocks) takes about 20 minutes, because requests to Yahoo are paced; the page fills in by itself, 100 stocks at a time. Stop with Ctrl+C.

### Start automatically on macOS

```bash
deploy/macos-autostart.sh install     # runs in the background now and at every login
deploy/macos-autostart.sh status
deploy/macos-autostart.sh restart     # after a git pull
deploy/macos-autostart.sh uninstall
```

Logs go to `logs/server.log`. Keep the folder outside `~/Documents`, `~/Desktop` and `~/Downloads` (for example `~/sp500-screener`): macOS blocks background services from those folders unless you grant them Full Disk Access.

### Settings

Environment variables, for `./run.sh` or the autostart (`PORT=8080 deploy/macos-autostart.sh install`):

| Variable | Default | |
|---|---|---|
| `PORT` | `8000` | |
| `HOST` | `127.0.0.1` | only this computer can open the page. Set `0.0.0.0` to open it from your phone on the same Wi-Fi (`http://<your-mac's-ip>:8000`); then also set `SCREENS_TOKEN`. |
| `PRICE_INTERVAL` | `300` | seconds between price updates while the market is open |
| `FUNDAMENTALS_INTERVAL` | `21600` | how old (seconds) a stock's fundamentals may get before they are fetched again |
| `YAHOO_RPS` | `1` | Yahoo requests per second, for prices and fundamentals together |
| `PRICES_ALWAYS` | `0` | `1` keeps fetching prices when the market is closed |
| `SCREENS_TOKEN` | | if set, saving and deleting need this token; in the browser console run `localStorage.setItem("screener.token", JSON.stringify("<token>"))` once |
| `SCREENER_DATA_DIR` | `data` | where market data, the database and notes live |
| `SCREENER_DB` | `data/screener.db` | |
| `SCREENER_NOTES_DIR` | `data/notes` | |

## Where your data is

Everything is in `data/`, which git ignores (except `data/screens.json`, the built-in screens). Back up that folder to keep a copy, or use **Download everything (.json)** in the Settings tab.

| What | Where | |
|---|---|---|
| Saved screens | `data/screener.db`, table `screens` | `sqlite3 data/screener.db "select id, name, rules from screens"` |
| Watchlists | `data/screener.db`, table `watchlists` | each is `{name, items: [{ticker, addedAt, addedPrice}]}` |
| Notes | `data/notes/<id>.md` | one Markdown file per note (below) |
| Market data | `data/sp500.json`, `data/russell1000.json`, `fundamentals.json`, `prices.json`, `universes.json`, `index.json` | rebuilt by the server |
| Built-in screens | `data/screens.json` | edit it to change them; ids start with `b-` |

Each note file has a small front matter block, then the note:

```markdown
---
id: n-mvk7fc6yqkbl
pinned: true
created: 2026-10-23T00:00:59.002Z
updated: 2026-10-23T00:01:05.720Z
---

# AAPL
...
```

You can edit, add or delete these files with any editor while the server runs; reload the page to see the change. A file edited by hand counts as updated at that moment, so it wins over an older copy still open in a browser. A new `.md` file needs no front matter: its name (letters, digits, `-` and `_`) becomes its id.

Notes from older versions that were kept in the SQLite `notes` table are moved into `data/notes/` automatically the first time the server starts.

### Bringing data over from the old hosted site

The page on `localhost` can't see what the old GitHub Pages site kept in your browser or in a GitHub repository, so import it once:

1. On the old site, open **Settings → Download everything (.json)**. (Or, if you synced to a GitHub repository, clone it; you need its `screener/` folder.)
2. With the local server stopped or running, either works:

```bash
.venv/bin/python -m backend.import_backup ~/Downloads/screener-backup-2026-10-27.json
.venv/bin/python -m backend.import_backup ~/screener-data/screener      # a synced GitHub folder
```

Nothing is deleted, and running it again is harmless. Reload the page afterwards.

If the page can't reach the server, it keeps your changes in the browser and saves them once the server is back; the Settings tab and the notes panel say so.

## Text size

Settings → *Text size*. *Auto* (the default) picks 100% below 1800 px of window width, 115% up to 2400 px and 130% above that, for large monitors; or choose 100–175% yourself. Every font size and the widths tied to text scale together (CSS variable `--ts`).

## Advanced Analysis tab

The **Analysis** tab shows one company's revenue, profits and margins over the years. Type a ticker, pick one from your recent stocks or watchlists, or hover a stock's name in the Screener or Watchlist and choose *Advanced analysis*. `http://localhost:8000/#analysis/NVDA` opens a stock directly.

- **Profitability**: revenue, gross profit, operating income, net income and diluted EPS, each with year-over-year growth; gross, operating and net margins, with the change in percentage points (pp: 45.0% → 47.5% is +2.5 pp).
- Above the table: revenue (TTM), latest quarterly revenue growth, gross margin, net margin and debt/equity.

The server also computes balance-sheet and cash-flow metrics (current and non-current assets and liabilities, debt, equity, ratios; operating cash flow, capex, free cash flow, buybacks, dividends); they are hidden on the page. To show them, add `"balance"` and/or `"cash"` to `SHOWN_SECTIONS` in `index.html`.

*Annual* shows the last 4-5 fiscal years plus TTM (the last four quarters); *Quarterly* shows the quarters Yahoo provides (usually 5-6), with growth against the same quarter a year earlier (matched by date). Fiscal years are named after the year they end in, as companies do (NVIDIA's FY2026 ended January 2026). Hover a dotted label for its definition.

Statements are fetched from Yahoo Finance when you open a stock (`GET /api/analysis/<ticker>`, `backend/analysis.py`): 6 requests, which skip ahead of the background updates but respect a rate-limit pause. They are cached for a day in `data/analysis/<TICKER>.json`; *Refresh* fetches them again. If Yahoo can't be reached, the last cached copy is shown with a note. Banks and insurers don't report some items (gross profit, current assets); those rows are left out.

## Notes tab

The **Notes** tab is a Markdown notebook: headings, **bold**, lists, `- [ ]` task lists, tables, code blocks, links, and `$NVDA` to link a ticker. Pin a note to keep it at the top. Notes save automatically (about a second after you stop typing, and when you switch notes or leave the page). Every edit is also kept in the browser, so if a save fails it is retried and nothing is lost; the status next to the editor says where the note was saved and when.

## Layout

| Path | What it is |
|---|---|
| `index.html` | The whole frontend: plain HTML, CSS and JavaScript, no framework or build step. To add a section tab, add an entry to `TABS` in the script, a side panel with the matching id, and its rendering in `renderResults`. |
| `run.sh` | Starts the server (creates `.venv` on first run). |
| `deploy/macos-autostart.sh` | Runs the server in the background on macOS (launchd). |
| `backend/server.py` | The server: update schedule plus HTTP (standard library only). |
| `backend/universe.py` | The stock lists: S&P 500 (Wikipedia, GitHub CSV fallback) and Russell 1000 (holdings of the iShares Russell 1000 ETF, IWB). Cached in `data/universes.json`, refreshed daily. Add another list by registering it in `UNIVERSES`. |
| `backend/fundamentals.py` | Fundamentals for all stocks, fetched in parallel (8 at a time). |
| `backend/analysis.py` | Financial statements and metrics for the Advanced Analysis tab, fetched on demand and cached in `data/analysis/`. |
| `backend/prices.py` | Latest 1-minute prices, fetched in parallel batches of 50. |
| `backend/build.py` | Merges both into one file per list (`data/sp500.json`, `data/russell1000.json`) plus `data/index.json`, recalculating the price-based metrics. |
| `backend/screens_db.py` | Saved screens and watchlists in SQLite. |
| `backend/notes_store.py` | Notes as Markdown files. |
| `backend/update.py` | One-off refresh from the command line: `python -m backend.update fundamentals`, `prices --force`, or `all`. |
| `tests/` | Offline unit tests: `.venv/bin/python -m unittest discover -s tests`. |
| `Dockerfile` | Optional: run the server in a container instead (`docker run -p 8000:8000 -v screener-data:/app/data ...`). |

Server endpoints: `/` (the page), `/api/data/<list>` (data for `sp500` or `russell1000`), `/api/index` (available lists), `/api/health` (update times and last errors), `/api/storage` (where data is kept), and `/api/screens`, `/api/watchlists`, `/api/notes` (`GET` list, `PUT /api/<kind>/<id>` save, `DELETE /api/<kind>/<id>` delete).

## Data format

Fundamentals and prices are fetched once for every stock in any list (about 1,000 stocks; nearly all S&P 500 names are also in the Russell 1000). Each `data/<list>.json` stores rows as arrays to stay small:

```json
{
  "asOf": "2026-10-22T17:30:00Z",
  "pricesAsOf": "2026-10-22T17:30:00Z",
  "fundamentalsAsOf": "2026-10-22T17:00:00Z",
  "builtAt": "2026-10-22T17:30:05Z",
  "count": 503,
  "cols": ["ticker", "name", "sector", "price", "chg", "mktCap", "pe", "fpe", "peg", "revGrowth", "netMargin", "divYield"],
  "rows": [["AAPL", "Apple Inc.", "Information Technology", 262.1, 1.32, 3890.2, 39.7, 33.5, 2.64, 8.1, 24.3, 0.4], ...]
}
```

Units: `price` in USD; `chg` is the % change from the previous session's close; `mktCap` in billions of USD; `revGrowth`, `netMargin`, `divYield` in percent. `pe` (trailing 12-month GAAP EPS) and `peg` are null for negative earnings; `fpe` is the forward P/E on analysts' consensus EPS for the next 12 months, null when that estimate is missing or negative. `revGrowth` is the latest quarter compared with the same quarter a year earlier. `peg` uses the earnings growth implied by Yahoo's trailing PEG.

## Notes

- Yahoo Finance data through `yfinance` is free but unofficial, and Yahoo limits how many requests one IP address may make. All requests go through one pace (`backend/yahoo.py`, `YAHOO_RPS`); when Yahoo answers "Too Many Requests" everything pauses for 1 minute, then 2, 4, 8, up to 15, and picks up where it left off. A stock that fails keeps its previous values. If you still see the message often, lower `YAHOO_RPS` (e.g. `YAHOO_RPS=0.5 ./run.sh`) or raise `PRICE_INTERVAL`.
- Exchange holidays are not modelled; on those days prices simply don't change.
