# S&P 500 Screener

A stock screener for the S&P 500 and the Russell 1000 (pick the list at the top of the page). You build screens from rules on price, P/E, forward P/E, PEG, revenue growth, net margin, dividend yield and market cap, save them, and switch between them to compare results. Sections are tabs down the left side of the page (a bottom bar on phones); the second keeps your **watchlists**: star any stock (☆) or add it by ticker, and see the same live metrics plus the change since you added it. Open the site with `#watchlist` to land on that tab.

**Live site: https://tsai8890.github.io/sp500-screener-data/**

The backend runs in two ways, from the same Python code:

| | GitHub (running now) | Always-on server (`backend/server.py`) |
|---|---|---|
| Prices | every 10 min during US market hours (:03, :13, ...) | every minute during US market hours |
| Fundamentals | every 30 min (:07, :37) | every 30 min |
| Runs on | GitHub Actions, free | any Linux machine or VM |
| Page reads | `data/sp500.json` from GitHub Pages | `/api/sp500` from the server |

P/E, PEG, market cap and dividend yield depend on the share price, so they are recalculated from the latest price every time prices update. The slower fundamentals (EPS, implied growth, revenue growth, margins, dividends) refresh every 30 minutes.

## Layout

| Path | What it is |
|---|---|
| `index.html` | The whole frontend: plain HTML, CSS and JavaScript, no framework or build step. To add a section tab, add an entry to `TABS` in the script, a side panel with the matching id, and its rendering in `renderResults`. |
| `backend/universe.py` | The stock lists: S&P 500 (Wikipedia, GitHub CSV fallback) and Russell 1000 (holdings of the iShares Russell 1000 ETF, IWB). Cached in `data/universes.json`, refreshed daily. Add another list by registering it in `UNIVERSES`. |
| `backend/fundamentals.py` | Fundamentals for all stocks, fetched in parallel (8 at a time). |
| `backend/prices.py` | Latest 1-minute prices, fetched in parallel batches of 50. |
| `backend/build.py` | Merges both into one file per list (`data/sp500.json`, `data/russell1000.json`) plus `data/index.json`, recalculating the price-based metrics. |
| `backend/update.py` | Command line used by GitHub Actions. |
| `backend/server.py` | Always-on server: scheduler plus HTTP (standard library only). |
| `backend/screens_db.py` | Saved screens and watchlists in SQLite (`data/screener.db`), used by the server's `/api/screens` and `/api/watchlists`. |
| `data/screens.json` | The built-in screens everyone sees. Edit it to change them. |
| `data/` | `fundamentals.json`, `prices.json` and the merged `sp500.json` the page reads. |
| `.github/workflows/` | `fundamentals.yml` (every 30 min) and `prices.yml` (every 10 min in market hours). |
| `tests/` | Offline unit tests: `python -m unittest discover -s tests`. |
| `Dockerfile`, `deploy/` | Ways to run the always-on server. |

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

python -m backend.update fundamentals     # one-off refresh, writes data/
python -m backend.update prices --force   # prices even if the market is closed

python -m backend.server                  # always-on server: open http://localhost:8000
```

Server endpoints: `/` (the page), `/api/data/<list>` (data for `sp500` or `russell1000`), `/api/index` (available lists), `/api/health` (update times and last errors), and `/api/screens` for saved screens (`GET` list, `PUT /api/screens/<id>` save, `DELETE /api/screens/<id>` delete) and `/api/watchlists` (same verbs; each watchlist is `{name, items: [{ticker, addedAt, addedPrice}]}`).

## Notes

The **Notes** tab is a Markdown notebook: headings, **bold**, lists, `- [ ]` task lists, tables, code blocks, links, and `$NVDA` to link a ticker. Pin a note to keep it at the top. Notes save automatically (about a second after you stop typing, and when you switch notes or leave the page). Every edit is also kept in the browser, so if a save fails it is retried and nothing is lost; the status next to the editor says where the note was saved and when.

Where notes are stored: see **Saving to GitHub** below; otherwise the always-on server (SQLite table `notes` via `/api/notes`), otherwise the browser (localStorage) with *Export all notes (.md)* for backups.

## Saving to GitHub (screens, watchlists and notes)

Open the **Settings** tab (bottom of the left rail, or `#settings`) to keep everything permanently in a GitHub repository. Works on any copy of the page except the Claude preview (which saves to your Claude account instead).

1. Create a **private** repository, e.g. `your-name/screener-data` (anyone can read a public one; the page warns you).
2. Create a fine-grained token (GitHub → Settings → Developer settings → Fine-grained tokens) with access to **only that repository** and **Contents: Read and write**.
3. In Settings, enter the repository, a folder (default `screener`) and the token, then **Connect**. Whatever this browser already had is uploaded.

The repository then holds:

```
screener/
  screens.json       {"updated": ..., "screens": [{id, name, rules, sectors, created, updated}]}
  watchlists.json    {"updated": ..., "watchlists": [{id, name, items: [{ticker, addedAt, addedPrice}]}]}
  notes/<id>.md      one Markdown file per note, with front matter:
```

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

Every save is a commit, so GitHub keeps the full history. Connect the same repository and token on another device and it loads the same data; something deleted on one device disappears on the others at their next load. A copy always stays in the browser: if GitHub can't be reached, changes are kept and retried, and the Settings tab shows what is saved and when. *Download everything (.json)* there makes a full backup. The token is stored only in that browser and sent only to api.github.com.

## Where saved screens and watchlists live

| Where the page runs | Saved screens go to |
|---|---|
| Always-on server | SQLite, `data/screener.db` (tables `screens` and `watchlists`), shared by everyone using that server. Inspect with `sqlite3 data/screener.db "select id, name, rules from screens"`. |
| GitHub Pages, connected to GitHub in Settings | `screens.json` / `watchlists.json` in your repository (see above). |
| GitHub Pages, not connected | The visitor's browser (localStorage). Use **Export screens (JSON)** / **Import screens** in the screens panel to back them up or move them between devices. |

Built-in screens always come from `data/screens.json`. To make one of your screens a built-in for everyone, export it and copy it into that file's `builtin` list (ids starting with `b-`).

On a public server anyone can change the saved screens. Set `SCREENS_TOKEN` to require a token for saving and deleting; then, in your browser's console on the site, run `localStorage.setItem("screener.token", JSON.stringify("<token>"))` once.

Server settings (environment variables): `PORT` (8000), `HOST` (0.0.0.0), `ALLOWED_ORIGINS` (`*`, or a comma-separated list such as `https://tsai8890.github.io`), `PRICE_INTERVAL` (60 seconds), `FUNDAMENTALS_INTERVAL` (1800 seconds), `PRICES_ALWAYS` (set to 1 to fetch prices outside market hours), `SCREENS_TOKEN` (token required to save/delete screens), `SCREENER_DB` (SQLite path, default `data/screener.db`).

## Deploy the always-on server (every-minute prices)

Any small Linux VM works, for example Oracle Cloud's Always Free VM or Google Cloud's free e2-micro.

**With Docker**

```bash
git clone https://github.com/tsai8890/sp500-screener-data && cd sp500-screener-data
docker build -t sp500-screener .
docker run -d --restart unless-stopped -p 8000:8000 -v screener-data:/app/data sp500-screener
```

**Without Docker (systemd)**

```bash
git clone https://github.com/tsai8890/sp500-screener-data ~/sp500-screener-data && cd ~/sp500-screener-data
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
sudo cp deploy/sp500-screener.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sp500-screener
```

Open port 8000 in the VM's firewall, then visit `http://<vm-ip>:8000`. The server hosts the page itself, so nothing else is needed.

To make the GitHub Pages site read from the server instead, the server needs HTTPS (browsers block plain-HTTP requests from an HTTPS page). Put it behind a domain with HTTPS (for example Caddy, or a Cloudflare Tunnel), then add this line to the `<head>` of `index.html`:

```html
<meta name="screener-api" content="https://your-domain/api/sp500">
```

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

- Yahoo Finance data through `yfinance` is free but unofficial; heavy polling can get rate-limited. A stock that fails keeps its previous values. If too many fail, the run errors out and the previous files stay in place.
- GitHub may start scheduled runs late, so on GitHub prices are typically 10-20 minutes old during market hours.
- Exchange holidays are not modelled; on those days prices simply don't change.
