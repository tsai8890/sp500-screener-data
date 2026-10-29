"""SEC XBRL company facts -> financial statements (the shape in backend/statements.py).

What the SEC gives: every number a company has filed, tagged with a US-GAAP concept
name, a period (start-end for flows, a single date for balances), the form it came
from and the filing date. Turning that into statements takes four rules, the same
for every company:

1. Names: one concept can be filed under several tag names (revenue alone has
   five common ones), so each key lists its tags in order; for every period the
   first tag that has a value wins. Tags can change from year to year.
2. Restatements: when the same period is reported again (a later 10-Q shows last
   year's quarter too, or a filing is amended), the most recently filed value wins.
3. Single quarters: income statements give 3-month figures, but cash-flow
   statements only give the year so far (3, 6, 9 months). A quarter is then the
   difference of two consecutive year-to-date figures. There is no 10-Q for the
   fourth quarter: Q4 = full year (10-K) - first nine months.
4. Signs: the SEC reports payments as positive numbers; like Yahoo, cash going out
   (capex, buybacks, dividends) is stored as negative here.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "10-KT", "10-KT/A"}

# key -> tags in order of preference ("us-gaap" unless prefixed). Flows have a start and end;
# balances only an end. "unit": USD unless noted.
FLOW_TAGS = {
    "income": {
        "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                    "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
        "costOfRevenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices"],
        "grossProfit": ["GrossProfit"],
        "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
        "operatingIncome": ["OperatingIncomeLoss"],
        "netIncome": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
        "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
        "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
        "pretaxIncome": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                         "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                         "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
        "taxProvision": ["IncomeTaxExpenseBenefit"],
    },
    "cashflow": {
        "operatingCF": ["NetCashProvidedByUsedInOperatingActivities",
                        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
        "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
        "da": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
               "DepreciationAmortizationAndAccretionNet", "Depreciation"],
        "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
        "dividends": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    },
}
BALANCE_TAGS = {
    "cash": ["CashCashEquivalentsAndShortTermInvestments"],
    "_cashOnly": ["CashAndCashEquivalentsAtCarryingValue",
                  "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "_shortTermInv": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                      "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "receivables": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "inventory": ["InventoryNet"],
    "currentAssets": ["AssetsCurrent"],
    "totalAssets": ["Assets"],
    "currentLiabilities": ["LiabilitiesCurrent"],
    "totalLiabilities": ["Liabilities"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    # borrowings (leases not included); summed into totalDebt below
    "_ltdTotal": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"],
    "_ltdNoncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "_ltdCurrent": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "_shortBorrowings": ["ShortTermBorrowings"],
    "_commercialPaper": ["CommercialPaper"],
}
UNITS = {"eps": "USD/shares", "shares": "shares"}
NEGATE = {"capex", "buybacks", "dividends"}           # payments -> cash going out
NOT_SUBTRACTABLE = {"shares"}                         # averages: no Q4 = year - 9 months
KEEP_ANNUAL, KEEP_QUARTERS = 10, 12


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _kind(days: int) -> str | None:
    if 80 <= days <= 100:
        return "q"
    if 170 <= days <= 195:
        return "h"
    if 260 <= days <= 285:
        return "9m"
    if 340 <= days <= 380:
        return "fy"
    return None


def _facts(company: dict, tag: str, unit: str) -> list[dict]:
    taxonomy, _, name = tag.rpartition(":")
    node = (company.get("facts", {}).get(taxonomy or "us-gaap", {}) or {}).get(name or tag)
    if not node:
        return []
    return [f for f in node.get("units", {}).get(unit, []) if f.get("form") in FORMS and f.get("val") is not None]


def _latest(facts: list[dict], key) -> dict:
    """{period key: value}, the most recently filed value for each period (rule 2)."""
    best: dict = {}
    for f in facts:
        k = key(f)
        if k is None:
            continue
        if k not in best or (f.get("filed") or "") >= (best[k].get("filed") or ""):
            best[k] = f
    return {k: f["val"] for k, f in best.items()}


def _flow_series(company: dict, tags: list[str], unit: str) -> dict[tuple, float]:
    """{(start, end): value} for one concept, first tag per period wins (rule 1)."""
    out: dict[tuple, float] = {}
    for tag in tags:
        vals = _latest(_facts(company, tag, unit), lambda f: (f["start"], f["end"]) if f.get("start") else None)
        for k, v in vals.items():
            out.setdefault(k, v)
    return out


def _instant_series(company: dict, tags: list[str], unit: str = "USD") -> dict[str, float]:
    out: dict[str, float] = {}
    for tag in tags:
        vals = _latest(_facts(company, tag, unit), lambda f: None if f.get("start") else f["end"])
        for k, v in vals.items():
            out.setdefault(k, v)
    return out


def _quarters_and_years(series: dict[tuple, float], subtract: bool) -> tuple[dict, dict]:
    """(quarters {end: value}, years {end: value}) from a flow series (rule 3)."""
    quarters: dict[str, float] = {}
    years: dict[str, float] = {}
    by_start: dict[str, list] = defaultdict(list)   # year-to-date figures sharing a start date
    for (start, end), v in series.items():
        kind = _kind((_d(end) - _d(start)).days + 1)
        if kind == "q":
            quarters[end] = v                          # reported directly: always preferred
        if kind == "fy":
            years[end] = v
        if kind in ("q", "h", "9m", "fy"):
            by_start[start].append((end, v, kind))
    if subtract:
        for start, items in by_start.items():
            items.sort()
            # consecutive year-to-date figures from the same start: each step is one quarter
            prev_end, prev_v = None, None
            for end, v, kind in items:
                if prev_end is None:
                    if kind == "q":
                        prev_end, prev_v = end, v
                    continue
                gap = (_d(end) - _d(prev_end)).days
                if 80 <= gap <= 100 and end not in quarters:
                    quarters[end] = round(v - prev_v, 6)   # drop float noise from the subtraction
                prev_end, prev_v = end, v
    return quarters, years


def statements(company: dict, ticker: str = "", fetched_at: str | None = None) -> dict:
    """Company facts -> {"annual": {...}, "quarterly": {...}} with statements.KEYS."""
    annual = {"income": defaultdict(dict), "balance": defaultdict(dict), "cashflow": defaultdict(dict)}
    quarterly = {"income": defaultdict(dict), "balance": defaultdict(dict), "cashflow": defaultdict(dict)}

    for kind, keys in FLOW_TAGS.items():
        for key, tags in keys.items():
            series = _flow_series(company, tags, UNITS.get(key, "USD"))
            q, y = _quarters_and_years(series, subtract=key not in NOT_SUBTRACTABLE)
            sign = -1 if key in NEGATE else 1
            for end, v in q.items():
                quarterly[kind][end][key] = sign * v
            for end, v in y.items():
                annual[kind][end][key] = sign * v

    # balances at the end of every quarter and year we have
    inst = {key: _instant_series(company, tags) for key, tags in BALANCE_TAGS.items()}
    for view in (annual, quarterly):
        ends = set(view["income"]) | set(view["cashflow"])
        for end in ends:
            rec = {}
            for key, series in inst.items():
                v = series.get(end)
                if v is None:   # balance dates can differ by a day or two from the period end
                    v = next((series[e] for e in series if abs((_d(e) - _d(end)).days) <= 3), None)
                if v is not None:
                    rec[key] = v
            if rec:
                view["balance"][end] = _balance(rec)

    # numbers the SEC doesn't have as such, worked out from its own figures so that a
    # merged statement never mixes, say, Yahoo's free cash flow with SEC cash flows
    for view in (annual, quarterly):
        for end in set(view["income"]) | set(view["cashflow"]):
            inc, cf = view["income"][end], view["cashflow"][end]
            if "grossProfit" not in inc and "revenue" in inc and "costOfRevenue" in inc:
                inc["grossProfit"] = round(inc["revenue"] - inc["costOfRevenue"], 6)
            if "operatingIncome" in inc and "da" in cf:
                inc["ebitda"] = round(inc["operatingIncome"] + cf["da"], 6)
            if "operatingCF" in cf and "capex" in cf:
                cf["fcf"] = round(cf["operatingCF"] + cf["capex"], 6)   # capex is negative

    def trim(view, keep):
        ends = sorted(set(view["income"]) | set(view["cashflow"]) | set(view["balance"]))[-keep:]
        return {k: {e: dict(view[k][e]) for e in ends if e in view[k] and view[k][e]} for k in view}

    return {"ticker": ticker, "fetchedAt": fetched_at, "source": "sec",
            "entityName": company.get("entityName"),
            "annual": trim(annual, KEEP_ANNUAL), "quarterly": trim(quarterly, KEEP_QUARTERS),
            "lastFiled": _last_filed(company)}


def _balance(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    if "cash" not in out and "_cashOnly" in rec:
        out["cash"] = rec["_cashOnly"] + rec.get("_shortTermInv", 0)
    ltd = rec.get("_ltdTotal")
    if ltd is None and ("_ltdNoncurrent" in rec or "_ltdCurrent" in rec):
        ltd = rec.get("_ltdNoncurrent", 0) + rec.get("_ltdCurrent", 0)
    short = rec.get("_shortBorrowings", 0) + rec.get("_commercialPaper", 0)
    if ltd is not None or short:
        out["totalDebt"] = (ltd or 0) + short
    return out


def _last_filed(company: dict) -> str | None:
    """Date of the newest 10-Q / 10-K in the facts (to know when there is something new)."""
    best = None
    for tax in company.get("facts", {}).values():
        for node in (tax or {}).values():
            for facts in node.get("units", {}).values():
                for f in facts:
                    if f.get("form") in FORMS and f.get("filed") and (best is None or f["filed"] > best):
                        best = f["filed"]
    return best

