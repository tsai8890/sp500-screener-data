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

from backend import analysis, build, common, fundamentals, notes_store, prices, screens_db, universe, yahoo  # noqa: E402

AAPL_INFO = {
    "currentPrice": 260.0, "marketCap": 3.9e12, "trailingPE": 39.4, "trailingPegRatio": 2.62,
    "trailingEps": 6.6, "revenueGrowth": 0.081, "profitMargins": 0.243, "dividendRate": 1.04,
    "regularMarketPreviousClose": 250.0, "forwardEps": 7.8,
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
        self.assertEqual(r["fpe"], round(286 / 7.8, 1))
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
        self.assertIsNone(r["fpe"])
        self.assertIsNone(r["chg"])   # no previous close known


class Prices(unittest.TestCase):
    def test_multiindex_frame(self):
        cols = pd.MultiIndex.from_product([["Close", "Open"], ["AAPL", "BRK-B"]])
        df = pd.DataFrame([[1.0, 2.0, 0, 0], [1.5, None, 0, 0]], columns=cols)
        self.assertEqual(prices._last_prices(df, ["AAPL", "BRK-B"]), {"AAPL": 1.5, "BRK-B": 2.0})

    def test_flat_frame(self):
        df = pd.DataFrame({"Close": [3.0, 3.25], "Open": [3, 3]})
        self.assertEqual(prices._last_prices(df, ["MSFT"]), {"MSFT": 3.25})

    @staticmethod
    def bars(times_prices):
        idx = pd.DatetimeIndex([pd.Timestamp(t, tz="America/New_York") for t, _ in times_prices])
        return pd.Series([p for _, p in times_prices], index=idx)

    def test_after_hours_price_kept_apart(self):
        reg, ext, _ = prices._split(self.bars([("2026-10-27 15:58", 100.0), ("2026-10-27 15:59", 101.0),
                                            ("2026-10-27 16:30", 103.0), ("2026-10-27 19:59", 104.5)]))
        self.assertEqual(reg, 101.0)
        self.assertEqual(ext[:2], (104.5, "post"))
        self.assertEqual(ext[2], "2026-10-27T23:59:00Z")

    def test_pre_market_next_morning(self):
        reg, ext, _ = prices._split(self.bars([("2026-10-27 15:59", 101.0), ("2026-10-27 17:00", 102.0),
                                            ("2026-10-28 07:15", 99.0)]))
        self.assertEqual((reg, ext[:2]), (101.0, (99.0, "pre")))

    def test_no_extended_price_while_market_is_open(self):
        reg, ext, _ = prices._split(self.bars([("2026-10-28 08:00", 99.0), ("2026-10-28 10:15", 100.5)]))
        self.assertEqual((reg, ext), (100.5, None))

    def test_previous_close_from_the_day_before(self):
        reg, ext, prev = prices._split(self.bars([("2026-10-27 15:59", 98.0), ("2026-10-27 17:00", 97.0),
                                                  ("2026-10-28 08:00", 99.0), ("2026-10-28 10:15", 100.5)]))
        self.assertEqual((reg, ext, prev), (100.5, None, 98.0))
        data = build.combine({"stocks": {"X": {"shares": 1e9, "eps": 5, "prevClose": 50}}, "order": ["X"]},
                             {"prices": {"X": 100.5}, "prev": {"X": 98.0}, "ext": {"X": [101.5, "post", "t"]}})
        row = dict(zip(data["cols"], data["rows"][0]))
        self.assertEqual(row["chg"], round((100.5 / 98 - 1) * 100, 2))   # not the stale 50 from fundamentals
        self.assertEqual(row["pe"], round(101.5 / 5, 1))                 # P/E on the latest (after-hours) price

    def test_update_stores_extended_and_build_adds_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prices.json")
            orig = prices._download
            prices._download = lambda syms: {s: (100.0, (102.0, "post", "2026-10-27T23:00:00Z")) for s in syms}
            try:
                out = prices.update(["AAPL"], path=path)
            finally:
                prices._download = orig
            self.assertEqual(out["ext"]["AAPL"][:2], [102.0, "post"])
            fund = {"stocks": {"AAPL": {"name": "Apple", "sector": "Tech", "shares": 1e9, "eps": 5, "price": 99, "prevClose": 98}}, "order": ["AAPL"]}
            data = build.combine(fund, out)
            row = dict(zip(data["cols"], data["rows"][0]))
            self.assertEqual((row["xPrice"], row["xChg"], row["xSession"]), (102.0, 2.0, "post"))

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


class Watchlists(unittest.TestCase):
    def test_crud_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "s.db")
            out = screens_db.upsert_watchlist("w-1", {"name": "Tech", "items": [
                {"ticker": "nvda", "addedAt": 1, "addedPrice": 200}, {"ticker": "NVDA"}, {"ticker": "BRK.B"}]}, path=db)
            self.assertEqual([i["ticker"] for i in out["items"]], ["NVDA", "BRK.B"])
            self.assertEqual(out["items"][0]["addedPrice"], 200.0)
            self.assertEqual(screens_db.list_watchlists(path=db)[0]["name"], "Tech")
            self.assertTrue(screens_db.delete_watchlist("w-1", path=db))
            self.assertEqual(screens_db.list_watchlists(path=db), [])
        for body in [{"items": [{"ticker": "DROP TABLE"}]}, {"items": "AAPL"}, {"items": [{"ticker": ""}]}]:
            with self.assertRaises(screens_db.InvalidScreen):
                screens_db.validate_watchlist("w", body)


class Notes(unittest.TestCase):
    T = 1_761_000_000_000   # an Oct 2025 timestamp in ms

    def test_save_pin_stale_and_delete(self):
        T = self.T
        with tempfile.TemporaryDirectory() as d:
            md = "# NVDA thesis\n\n- data center **growth**\n- watch margins"
            out = notes_store.upsert_note("n-1", {"body": md, "pinned": True, "created": T, "updated": T + 100}, folder=d)
            self.assertEqual((out["body"], out["pinned"], out["updated"]), (md, True, T + 100))
            with open(os.path.join(d, "n-1.md"), encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(text.startswith("---\nid: n-1\npinned: true\n"))
            self.assertTrue(text.endswith("\n---\n\n" + md))
            stale = notes_store.upsert_note("n-1", {"body": "old text", "updated": T + 50}, folder=d)   # older copy loses
            self.assertTrue(stale["stale"])
            got = notes_store.list_notes(folder=d)[0]
            self.assertEqual((got["body"], got["created"], got["updated"]), (md, T, T + 100))
            notes_store.upsert_note("n-2", {"body": "second", "updated": T + 200}, folder=d)
            self.assertEqual([n["id"] for n in notes_store.list_notes(folder=d)], ["n-1", "n-2"])   # pinned first
            self.assertTrue(notes_store.delete_note("n-1", folder=d))
            self.assertFalse(notes_store.delete_note("../n-2", folder=d))
            self.assertEqual([n["id"] for n in notes_store.list_notes(folder=d)], ["n-2"])
        for nid, body in [("../x", {}), ("ok", {"body": 5}), ("ok", {"body": "x" * 200_001})]:
            with self.assertRaises(screens_db.InvalidScreen):
                screens_db.validate_note(nid, body)

    def test_files_edited_by_hand(self):
        with tempfile.TemporaryDirectory() as d:
            notes_store.upsert_note("n-1", {"body": "v1", "updated": self.T}, folder=d)
            path = os.path.join(d, "n-1.md")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\nadded in an editor")
            got = notes_store.list_notes(folder=d)[0]
            self.assertEqual(got["body"], "v1\nadded in an editor")
            self.assertGreater(got["updated"], self.T)   # the edit counts as newer
            with open(os.path.join(d, "ideas.md"), "w", encoding="utf-8") as f:   # plain file, no front matter
                f.write("# Ideas\n\n$AAPL")
            open(os.path.join(d, "bad name.md"), "w").close()   # not a valid id: ignored
            ideas = [n for n in notes_store.list_notes(folder=d) if n["id"] == "ideas"]
            self.assertEqual(ideas[0]["body"], "# Ideas\n\n$AAPL")
            self.assertEqual(len(notes_store.list_notes(folder=d)), 2)

    def test_migrate_from_sqlite(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "s.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT, pinned INTEGER, created INTEGER, updated INTEGER)")
            conn.execute("INSERT INTO notes VALUES ('n-old', '# Old', 1, ?, ?)", (self.T, self.T + 5))
            conn.commit(); conn.close()
            folder = os.path.join(d, "notes")
            self.assertEqual(notes_store.migrate_from_sqlite(db, folder=folder), 1)
            self.assertEqual(notes_store.migrate_from_sqlite(db, folder=folder), 0)   # only once
            n = notes_store.list_notes(folder=folder)[0]
            self.assertEqual((n["id"], n["body"], n["pinned"], n["updated"]), ("n-old", "# Old", True, self.T + 5))


class Universes(unittest.TestCase):
    def test_clean_and_union(self):
        df = pd.DataFrame({"Symbol": ["AAPL", "brk.b", "AAPL", None], "Company": ["Apple", "Berkshire", "dup", "x"],
                           "GICS Sector": ["IT", "Fin", "IT", None]})
        rows = universe._clean(df, "Symbol", "Company", "GICS Sector")
        self.assertEqual(rows, [("AAPL", "Apple", "IT"), ("BRK.B", "Berkshire", "Fin")])
        cache = {"universes": {"russell1000": {"stocks": [["AAPL", "Apple R", "x"], ["CRWD", "CrowdStrike", "IT"]]},
                               "sp500": {"stocks": [["AAPL", "Apple Inc.", "IT"], ["MSFT", "Microsoft", "IT"]]}}}
        self.assertEqual(universe.union(cache), [("AAPL", "Apple Inc.", "IT"), ("MSFT", "Microsoft", "IT"),
                                                 ("CRWD", "CrowdStrike", "IT")])

    def test_build_writes_one_file_per_universe(self):
        f = fundamentals.extract(AAPL_INFO)
        fund = {"asOf": "t", "order": ["AAPL", "CRWD"], "stocks": {"AAPL": {"name": "Apple", "sector": "IT", **f},
                                                                    "CRWD": {"name": "Crowd", "sector": "IT", **f}}}
        lists = {"universes": {"sp500": {"name": "S&P 500", "stocks": [["AAPL"]]},
                               "russell1000": {"name": "Russell 1000", "stocks": [["AAPL"], ["CRWD"]]}}}
        with tempfile.TemporaryDirectory() as d:
            orig = (build.universe_path, build.INDEX_PATH)
            build.universe_path, build.INDEX_PATH = (lambda u: os.path.join(d, u + ".json")), os.path.join(d, "index.json")
            try:
                outs = build.build(fund=fund, prices={"prices": {}}, universes=lists)
            finally:
                build.universe_path, build.INDEX_PATH = orig
            self.assertEqual({u: o["count"] for u, o in outs.items()}, {"sp500": 1, "russell1000": 2})
            idx = common.read_json(os.path.join(d, "index.json"))
            self.assertEqual([u["id"] for u in idx["universes"]], ["sp500", "russell1000"])
            self.assertEqual(common.read_json(os.path.join(d, "russell1000.json"))["universe"], "Russell 1000")


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



class RateLimit(unittest.TestCase):
    def test_detects_yahoo_rate_limit(self):
        class YFRateLimitError(Exception):
            pass
        self.assertTrue(yahoo.is_rate_limit(YFRateLimitError("Too Many Requests. Rate limited. Try after a while.")))
        self.assertTrue(yahoo.is_rate_limit("YFRateLimitError('Too Many Requests')"))
        self.assertFalse(yahoo.is_rate_limit(KeyError("currentPrice")))

    def test_pause_grows_and_shrinks(self):
        g = yahoo.Gate(rps=1000)
        g.limited(); self.assertEqual(g.pause, 60); self.assertGreater(g.paused_for(), 59)
        g.limited(); self.assertEqual(g.pause, 60)          # same pause, not stacked
        g.until = 0; g.limited(); self.assertEqual(g.pause, 120)
        for _ in range(9):
            g.until = 0; g.limited()
        self.assertEqual(g.pause, yahoo.MAX_PAUSE)
        g.until = 0; g.ok(); self.assertEqual(g.pause, yahoo.MAX_PAUSE // 2)


class IncrementalFundamentals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fundamentals.json")
        self.stocks = [("AAPL", "Apple", "Tech"), ("MSFT", "Microsoft", "Tech"), ("XOM", "Exxon", "Energy")]
        self.orig = (universe.load, universe.union, fundamentals.fetch_one, yahoo.GATE)
        universe.load = lambda *a, **k: {}
        universe.union = lambda cache: list(self.stocks)
        self.calls = []

    def tearDown(self):
        universe.load, universe.union, fundamentals.fetch_one, yahoo.GATE = self.orig

    def fetch(self, fail=()):
        def f(t, tries=3):
            self.calls.append(t)
            return None if t in fail else fundamentals.extract(AAPL_INFO)
        fundamentals.fetch_one = f

    def test_resumes_and_only_fetches_what_is_due(self):
        self.fetch(fail={"XOM"})
        out = fundamentals.update(self.path, max_age=3600)
        self.assertEqual(sorted(out["stocks"]), ["AAPL", "MSFT"])
        self.assertEqual(out["missing"], ["XOM"]); self.assertEqual(out["pending"], 0)
        raw = common.read_json(self.path); raw["failedAt"]["XOM"] = "2020-01-01T00:00:00Z"; common.write_json(self.path, raw)
        self.calls.clear(); self.fetch()
        out = fundamentals.update(self.path, max_age=3600)     # a while later only XOM is due
        self.assertEqual(self.calls, ["XOM"]); self.assertEqual(out["pending"], 0)
        self.assertEqual(sorted(out["stocks"]), ["AAPL", "MSFT", "XOM"])
        self.calls.clear()
        self.assertEqual(fundamentals.due(common.read_json(self.path), 3600), [])
        fundamentals.update(self.path, max_age=3600); self.assertEqual(self.calls, [])

    def test_failed_stock_keeps_previous_values(self):
        self.fetch(); fundamentals.update(self.path)
        self.fetch(fail={"MSFT"}); out = fundamentals.update(self.path)
        self.assertIn("MSFT", out["stocks"]); self.assertEqual(out["stale"], ["MSFT"])

    def test_dead_tickers_are_not_an_error_and_wait_before_retry(self):
        self.fetch(fail={"XOM"}); fundamentals.update(self.path, max_age=3600)
        self.calls.clear()
        out = fundamentals.update(self.path, max_age=3600)      # XOM failed just now: not due again yet
        self.assertEqual(self.calls, []); self.assertIn("XOM", out["failedAt"])
        self.fetch(fail={"AAPL", "MSFT", "XOM"})
        fundamentals.update(self.path, max_age=0)              # a few failures: no exception

    def test_everything_failing_means_blocked(self):
        orig = fundamentals.BLOCKED_MIN; fundamentals.BLOCKED_MIN = 3
        try:
            self.fetch(fail={"AAPL", "MSFT", "XOM"})
            with self.assertRaises(RuntimeError):
                fundamentals.update(self.path)
        finally:
            fundamentals.BLOCKED_MIN = orig

    def test_success_clears_failure(self):
        self.fetch(fail={"XOM"}); fundamentals.update(self.path, max_age=3600)
        self.fetch(); out = fundamentals.update(self.path, max_age=0)
        self.assertNotIn("XOM", out["failedAt"]); self.assertIn("XOM", out["stocks"])

    def test_old_files_without_fetched_at_use_file_time(self):
        common.write_json(self.path, {"asOf": "2020-01-01T00:00:00Z", "stocks": {"AAPL": {"price": 1}}})
        due = [t for t, _, _ in fundamentals.due(common.read_json(self.path), 3600)]
        self.assertEqual(due, ["MSFT", "XOM", "AAPL"])   # never fetched first, then the oldest
        common.write_json(self.path, {"asOf": common.iso(common.utc_now()), "stocks": {"AAPL": {"price": 1}}})
        self.assertNotIn("AAPL", [t for t, _, _ in fundamentals.due(common.read_json(self.path), 3600)])


def statement(rows: dict, ends: list[str]) -> pd.DataFrame:
    """A yfinance-style statement: line items as rows, newest period first."""
    cols = [pd.Timestamp(e) for e in ends]
    return pd.DataFrame({c: {k: v[i] for k, v in rows.items()} for i, c in enumerate(cols)})


class FakeStatements:
    A = ["2025-09-30", "2024-09-30", "2023-09-30"]
    Q = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]

    def __init__(self, sym):
        self.sym = sym
        self.calls = []

    def _rec(self, name):
        self.calls.append(name)

    @property
    def income_stmt(self):
        self._rec("income_stmt")
        return statement({"Total Revenue": [400, 380, 360], "Cost Of Revenue": [220, 215, 210],
                          "Operating Income": [120, 110, 100], "Net Income Common Stockholders": [100, 95, 90],
                          "EBITDA": [140, 130, 120], "Diluted EPS": [6.6, 6.1, 5.7], "Diluted Average Shares": [15, 15.5, 16],
                          "Research And Development": [30, 28, 26]}, self.A)

    @property
    def quarterly_income_stmt(self):
        self._rec("quarterly_income_stmt")
        return statement({"Total Revenue": [110, 100, 120, 102, 95], "Gross Profit": [50, 45, 55, 46, 42],
                          "Operating Income": [33, 30, 36, 31, 28], "Net Income Common Stockholders": [28, 25, 30, 26, 24],
                          "Diluted EPS": [1.9, 1.7, 2.0, 1.7, 1.6], "Diluted Average Shares": [14.8, 14.9, 15, 15, 15.1]}, self.Q)

    @property
    def balance_sheet(self):
        self._rec("balance_sheet")
        return statement({"Current Assets": [150, 140, 130], "Total Assets": [360, 350, 340], "Inventory": [10, 9, 8],
                          "Current Liabilities": [160, 150, 140], "Total Liabilities Net Minority Interest": [290, 280, 270],
                          "Total Debt": [100, 110, 120], "Cash Cash Equivalents And Short Term Investments": [60, 55, 50],
                          "Stockholders Equity": [70, 70, 70]}, self.A)

    @property
    def quarterly_balance_sheet(self):
        self._rec("quarterly_balance_sheet")
        return statement({"Current Assets": [155, 150, 148, 150, 145], "Total Assets": [365, 362, 361, 360, 355],
                          "Current Liabilities": [150, 155, 158, 160, 150], "Stockholders Equity": [75, 72, 71, 70, 69],
                          "Total Debt": [95, 97, 99, 100, 101], "Cash Cash Equivalents And Short Term Investments": [65, 62, 61, 60, 58]}, self.Q)

    @property
    def cashflow(self):
        self._rec("cashflow")
        return statement({"Operating Cash Flow": [120, 110, 105], "Capital Expenditure": [-12, -11, -10],
                          "Stock Based Compensation": [10, 9, 8], "Repurchase Of Capital Stock": [-90, -80, -70]}, self.A)

    @property
    def quarterly_cashflow(self):
        self._rec("quarterly_cashflow")
        return statement({"Operating Cash Flow": [32, 28, 35, 30, 27], "Free Cash Flow": [29, 25, 31, 27, 24]}, self.Q)


class AnalysisDates(unittest.TestCase):
    def test_fiscal_year_named_after_its_end(self):
        self.assertEqual(analysis._label("2026-01-25", False), "FY2026")
        self.assertEqual(analysis._label("2025-09-30", False), "FY2025")

    def test_growth_skips_a_missing_quarter(self):
        q = {"income": {e: {"revenue": v} for e, v in
                        [("2026-06-30", 200), ("2026-03-31", 150), ("2025-09-30", 120), ("2025-06-30", 100), ("2025-03-31", 90)]}}
        out = analysis.compute({"ticker": "X", "annual": {}, "quarterly": q})["quarterly"]
        rev = out["sections"][0]["rows"][0]
        self.assertEqual(rev["growth"][-1], 100.0)   # Jun 2026 vs Jun 2025, not vs Mar 2025
        self.assertAlmostEqual(rev["growth"][-2], 66.67, places=2)   # Mar 2026 vs Mar 2025


class Analysis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig = (analysis.CACHE_DIR, getattr(analysis.yf, "Ticker", None), analysis.GATE)
        analysis.CACHE_DIR = self.tmp
        self.made = []
        def ticker(sym):
            t = FakeStatements(sym); self.made.append(t); return t
        analysis.yf.Ticker = ticker
        analysis.GATE = yahoo.Gate(rps=1000)

    def tearDown(self):
        analysis.CACHE_DIR, analysis.yf.Ticker, analysis.GATE = self.orig

    def row(self, view, key):
        for sec in view["sections"]:
            for r in sec["rows"]:
                if r["key"] == key:
                    return r
        return None

    def test_annual_metrics_growth_and_ttm(self):
        out = analysis.get("aapl")
        a = out["annual"]
        self.assertEqual([p["label"] for p in a["periods"]], ["FY2023", "FY2024", "FY2025", "TTM"])
        rev = self.row(a, "revenue")
        self.assertEqual(rev["values"], [360, 380, 400, 432])            # TTM = last four quarters
        self.assertAlmostEqual(rev["growth"][2], 5.26, places=2)
        self.assertIsNone(rev["growth"][3])
        self.assertEqual(self.row(a, "grossProfit")["values"][:3], [150, 165, 180])   # derived from cost of revenue
        self.assertAlmostEqual(self.row(a, "grossMargin")["values"][2], 45.0)
        self.assertAlmostEqual(self.row(a, "currentRatio")["values"][2], 0.9375)
        self.assertEqual(self.row(a, "fcf")["values"][:3], [95, 99, 108])          # OCF + capex
        self.assertEqual(self.row(a, "netDebt")["values"][2], 40)
        self.assertAlmostEqual(self.row(a, "netDebtToEbitda")["values"][2], 40 / 140, places=4)
        # no tax data in the fixture: 21% tax; invested capital = debt 100 + equity 70
        self.assertAlmostEqual(self.row(a, "roic")["values"][2], 120 * 0.79 / 170 * 100, places=2)
        self.assertEqual(self.row(a, "nonCurrentAssets")["values"][2], 210)
        self.assertEqual([s["id"] for s in a["sections"]], ["profit", "balance", "cash"])

    def test_quarterly_growth_is_year_over_year(self):
        q = analysis.get("AAPL")["quarterly"]
        self.assertEqual(q["periods"][-1]["label"], "Jun 2026")
        g = self.row(q, "revenue")["growth"]
        self.assertEqual(g[:4], [None, None, None, None])
        self.assertAlmostEqual(g[4], (110 - 95) / 95 * 100, places=2)
        self.assertIsNone(self.row(q, "roe"))   # removed from the page

    def test_cached_for_a_day(self):
        analysis.get("MSFT")
        analysis.get("MSFT")
        analysis.get("MSFT", refresh=True)       # younger than MIN_REFRESH: still cached
        self.assertEqual(len(self.made), 1)
        self.assertEqual(len(self.made[0].calls), 6)

    def test_rate_limited_uses_old_copy(self):
        analysis.get("NVDA")
        path = analysis._cache_path("NVDA"); raw = common.read_json(path)
        raw["fetchedAt"] = "2020-01-01T00:00:00Z"; common.write_json(path, raw)
        analysis.GATE.limited()
        out = analysis.get("NVDA")
        self.assertIn("limiting", out["warning"])
        analysis.GATE.until = 0
        with self.assertRaises(analysis.RateLimited):
            analysis.GATE.limited(); analysis.get("AMD")

    def test_rejects_bad_ticker_and_empty_statements(self):
        with self.assertRaises(ValueError):
            analysis.get("../etc")
        class Empty:
            def __getattr__(self, name): return pd.DataFrame()
        analysis.yf.Ticker = lambda sym: Empty()
        with self.assertRaises(LookupError):
            analysis.get("ZZZZ")

if __name__ == "__main__":
    unittest.main()
