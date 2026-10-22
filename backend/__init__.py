"""S&P 500 screener backend.

Modules:
  common        paths, JSON helpers, US market hours
  universe      S&P 500 constituents (Wikipedia, with a GitHub CSV fallback)
  fundamentals  per-stock fundamentals from Yahoo Finance, fetched in parallel
  prices        latest prices for every stock, fetched in parallel batches
  build         merges fundamentals + prices into data/sp500.json for the page
  update        command line: python -m backend.update {fundamentals|prices|all}
  server        always-on server: prices every minute, fundamentals every 30 min
"""
