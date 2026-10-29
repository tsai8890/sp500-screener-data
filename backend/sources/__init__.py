"""Data sources: one module per provider. Each one only fetches and translates data
into the shapes the rest of the backend uses; no business logic lives here.

  yahoo     Yahoo Finance (via yfinance): shared rate limit, financial statements
  finnhub   Finnhub: fast quotes for the stocks on screen (needs FINNHUB_API_KEY)
  sec       SEC EDGAR: HTTP client, ticker -> CIK, company facts, new-filings feed
            (needs SEC_USER_AGENT)
  sec_xbrl  SEC XBRL company facts -> financial statements

Statements from every source use the same shape (see backend/statements.py):

  {"ticker": "GOOG", "fetchedAt": iso, "source": "sec" | "yahoo",
   "annual":    {"income": {end: {key: value}}, "balance": {...}, "cashflow": {...}},
   "quarterly": {...same...}}

with the keys listed in statements.KEYS. To add a source, write a module with a
function returning that shape and add it to statements.PROVIDERS.
"""
