"""Offline tests for the backend: no network needed.

  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "yfinance" not in sys.modules:
    try:
        import yfinance  # noqa: F401
    except ImportError:  # allow running the tests without yfinance installed
        sys.modules["yfinance"] = types.ModuleType("yfinance")

import pandas as pd  # noqa: E402

from backend import build, common, fundamentals, prices, screens_db  # noqa: E402

AAPL_INFO = {
    "currentPrice": 260.0, "marketCap": 3.9e12, "trailingPE": 39.4, "trailingPegRatio": 2.62,
    "trailingEps": 6.6, "revenueGrowth": 0.081, "profitMargins": 0.243, "dividendRate": 1.04,
    "regularMarketPreviousClose": 250.0,
}


class Extract(unittest.TestCase):
    def test_fields(self):
        f = fundamentals.extract(AAPL_INFO)
        self.assertAlmostEqual(f["shares"], 1.5e10)
        self.assertAlmostEqual(f["growth"], round(39.4 / 2.62, 3))
        self.assertEqual(f["revGrowth"], 8.1)
        self.assertEqual(f["netMargin"], 24.3)
        self.assertEqual(f["divRate"], 1.04)

    def test_negative_earnings_have_no_growth(self):
        f = fundamentals.extract({"currentPrice": 20, "marketCap": 2e10, "trailingPE": -5, "trailingEps": -4})
        self.assertIsNone(f["growth"])
        self.assertEqual(f["eps"], -4)

    def test_missing_price_is_rejected(self):
        self.assertIsNone(fundamentals.extract({"marketCap": 1e9}))


class Combine(unittest.TestCase):
    def setUp(self):
        f = fundamentals.extract(AAPL_INFO)
        self.fund = {"asOf": "2026-10-22T16:00:00Z", "order": ["AAPL", "NEG"],
                     "stocks": {"AAPL": {"name": "Apple", "sector": "IT", **f},
                                "NEG": {"name": "Neg", "sector": "Energy", **fundamentals.extract(
                                    {"currentPrice": 20, "marketCap": 2e10, "trailingEps": -4})}}}

    def test_recomputes_from_new_price(self):
        out = build.combine(self.fund, {"asOf": "2026-10-22T17:00:00Z", "prices": {"AAPL": 286.0}})
        r = dict(zip(out["cols"], out["rows"][0]))
        self.assertEqual(r["price"], 286.0)
        self.assertEqual(r["chg"], round((286 / 250 - 1) * 100, 2))
        self.assertEqual(r["pe"], round(286 / 6.6, 1))
        self.assertEqual(r["mktCap"], round(1.5e10 * 286 / 1e9, 1))
        self.assertEqual(r["divYield"], round(1.04 / 286 * 100, 2))
        self.assertAlmostEqual(r["peg"], round((286 / 6.6) / (39.4 / 2.62), 2))
        self.assertEqual(out["asOf"], "2026-10-22T17:00:00Z")
        self.assertEqual(out["fundamentalsAsOf"], "2026-10-22T16:00:00Z")

    def test_falls_back_to_fundamentals_price(self):
        out = build.combine(self.fund, None)
        r = dict(zip(out["cols"], out["rows"][0]))
        self.assertEqual(r["price"], 260.0)
        self.assertEqual(out["asOf"], "2026-10-22T16:00:00Z")

    def test_negative_earnings(self):
        out = build.combine(self.fund, {"asOf": "x", "prices": {}})
        r = dict(zip(out["cols"], out["rows"][1]))
        self.assertIsNone(r["pe"])
        self.assertIsNone(r["peg"])
        self.assertIsNone(r["chg"])   # no previous close known


class Prices(unittest.TestCase):
    def test_multiindex_frame(self):
        cols = pd.MultiIndex.from_product([["Close", "Open"], ["AAPL", "BRK-B"]])
        df = pd.DataFrame([[1.0, 2.0, 0, 0], [1.5, None, 0, 0]], columns=cols)
        self.assertEqual(prices._last_prices(df, ["AAPL", "BRK-B"]), {"AAPL": 1.5, "BRK-B": 2.0})

    def test_flat_frame(self):
        df = pd.DataFrame({"Close": [3.0, 3.25], "Open": [3, 3]})
        self.assertEqual(prices._last_prices(df, ["MSFT"]), {"MSFT": 3.25})

    def test_update_maps_symbols_and_keeps_previous(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prices.json")
            common.write_json(path, {"prices": {"XOM": 110.0}})
            orig = prices._download
            prices._download = lambda syms: {s: 100.0 for s in syms if s != "XOM"}
            try:
                out = prices.update(["BRK.B", "AAPL", "XOM"], path=path)
            finally:
                prices._download = orig
            self.assertEqual(out["prices"], {"BRK.B": 100.0, "AAPL": 100.0, "XOM": 110.0})
            self.assertEqual(out["fresh"], 2)


class Screens(unittest.TestCase):
    def test_crud(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "s.db")
            body = {"name": "Cheap growth", "sectors": ["Energy"], "created": 5,
                    "rules": [{"field": "pe", "op": "between", "value": "10", "value2": 20.5}]}
            out = screens_db.upsert("s-1", body, path=db)
            self.assertEqual(out["rules"], [{"field": "pe", "op": "between", "value": 10, "value2": 20.5}])
            self.assertEqual(out["created"], 5)
            screens_db.upsert("s-1", {**body, "name": "Renamed"}, path=db)
            got = screens_db.list_screens(path=db)
            self.assertEqual([(s["id"], s["name"], s["created"]) for s in got], [("s-1", "Renamed", 5)])
            self.assertTrue(screens_db.delete("s-1", path=db))
            self.assertEqual(screens_db.list_screens(path=db), [])

    def test_rejects_bad_input(self):
        for sid, body in [("../x", {}), ("ok", {"rules": [{"field": "evil", "op": ">", "value": 1}]}),
                          ("ok", {"rules": [{"field": "pe", "op": "drop", "value": 1}]}),
                          ("ok", {"rules": [{"field": "pe", "op": ">", "value": "abc"}]})]:
            with self.assertRaises(screens_db.InvalidScreen):
                screens_db.validate(sid, body)


class MarketHours(unittest.TestCase):
    def at(self, s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    def test_session(self):
        self.assertTrue(common.market_session(self.at("2026-10-22 14:00")))    # 10:00 EDT Thu
        self.assertFalse(common.market_session(self.at("2026-10-22 13:00")))   # 9:00 EDT
        self.assertTrue(common.market_session(self.at("2026-10-22 20:10")))    # 16:10 EDT, grace
        self.assertFalse(common.market_session(self.at("2026-10-22 20:20")))   # 16:20 EDT
        self.assertFalse(common.market_session(self.at("2026-10-24 15:00")))   # Saturday
        self.assertTrue(common.market_session(self.at("2026-12-01 15:00")))    # 10:00 EST


if __name__ == "__main__":
    unittest.main()
