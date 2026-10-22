"""Command line used by GitHub Actions (and handy locally).

  python -m backend.update fundamentals   # refresh fundamentals, then rebuild sp500.json
  python -m backend.update prices         # refresh prices (only while the market is open), rebuild
  python -m backend.update prices --force # refresh prices even when the market is closed
  python -m backend.update all            # both, then rebuild
"""
from __future__ import annotations

import argparse
import sys

from . import build, fundamentals, prices
from .common import market_session


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend.update")
    ap.add_argument("what", choices=["fundamentals", "prices", "all"])
    ap.add_argument("--force", action="store_true", help="fetch prices even outside market hours")
    args = ap.parse_args(argv)

    try:
        if args.what in ("fundamentals", "all"):
            fundamentals.update()
        if args.what in ("prices", "all"):
            if args.force or args.what == "all" or market_session():
                prices.update()
            else:
                print("Market closed: prices not fetched.")
                if args.what == "prices":
                    return 0
        for uid, out in build.build().items():
            print(f"Built {uid}.json: {out['count']} stocks, prices {out['pricesAsOf']}, "
                  f"fundamentals {out['fundamentalsAsOf']}")
        return 0
    except RuntimeError as e:
        print(f"Update failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
