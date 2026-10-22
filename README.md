# S&P 500 screener data

A free daily dataset for the S&P 500 Screener page.

- `fetch_sp500.py` gets the current S&P 500 list (from Wikipedia) and pulls market cap, P/E, PEG, revenue growth, net margin and dividend yield for each stock from Yahoo Finance using `yfinance`.
- `.github/workflows/update-data.yml` runs the script every weekday at 22:15 UTC, after the US market close, and commits `data/sp500.json`.
- A daily Claude scheduled task copies `data/sp500.json` into the screener page.

## Setup

1. Create a **public** GitHub repository. Actions minutes are free and unlimited on public repos.
2. Add these files, keeping the same paths.
3. Open the **Actions** tab, choose "Update S&P 500 data", then click **Run workflow** to create the first dataset. A run takes about 5–15 minutes.
4. Check that `data/sp500.json` appears in the repo.

## Notes

- Yahoo Finance data through `yfinance` is free but unofficial, and Yahoo can rate-limit or change it. If a stock fails to download, its previous row is kept. If fewer than 80% of stocks download fresh, the run fails and the old file stays in place.
- Revenue growth is the latest quarter compared with the same quarter a year earlier. PEG is Yahoo's trailing PEG. P/E is blank for companies with negative earnings.
