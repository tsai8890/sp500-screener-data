"""A small, realistic SEC companyfacts document (calendar fiscal year).

FY2024 (10-K), FY2025 Q1-Q3 (10-Qs) and its 10-K, FY2026 Q1-Q2 (10-Qs).
Revenue: 3-month figures each quarter, filed under SalesRevenueNet for 2024 and
RevenueFromContractWithCustomerExcludingAssessedTax afterwards (tag switch).
Operating cash flow: year-to-date only (3, 6, 9, 12 months), like real cash-flow statements.
Q2 2025 revenue is restated in a later filing (the later value must win).
"""


def fact(start, end, val, form, filed, fy=None, fp=None):
    f = {"end": end, "val": val, "form": form, "filed": filed, "accn": "0000-" + filed, "fy": fy, "fp": fp}
    if start:
        f["start"] = start
    return f


def facts():
    rev_old = [fact("2024-01-01", "2024-12-31", 360.0, "10-K", "2025-02-01")]
    rev = [
        fact("2025-01-01", "2025-03-31", 100.0, "10-Q", "2025-04-25"),
        fact("2025-04-01", "2025-06-30", 105.0, "10-Q", "2025-07-25"),
        fact("2025-04-01", "2025-06-30", 106.0, "10-Q", "2026-07-25"),   # restated a year later
        fact("2025-07-01", "2025-09-30", 110.0, "10-Q", "2025-10-25"),
        fact("2025-01-01", "2025-09-30", 316.0, "10-Q", "2025-10-25"),   # 9 months
        fact("2025-01-01", "2025-12-31", 440.0, "10-K", "2026-02-01"),   # Q4 = 440 - 316 = 124
        fact("2026-01-01", "2026-03-31", 120.0, "10-Q", "2026-04-25"),
        fact("2026-04-01", "2026-06-30", 130.0, "10-Q", "2026-07-25"),
    ]
    ocf = [  # year to date only
        fact("2025-01-01", "2025-03-31", 30.0, "10-Q", "2025-04-25"),
        fact("2025-01-01", "2025-06-30", 65.0, "10-Q", "2025-07-25"),
        fact("2025-01-01", "2025-09-30", 100.0, "10-Q", "2025-10-25"),
        fact("2025-01-01", "2025-12-31", 140.0, "10-K", "2026-02-01"),
        fact("2026-01-01", "2026-03-31", 40.0, "10-Q", "2026-04-25"),
        fact("2026-01-01", "2026-06-30", 85.0, "10-Q", "2026-07-25"),
    ]
    capex = [
        fact("2025-01-01", "2025-03-31", 5.0, "10-Q", "2025-04-25"),
        fact("2025-01-01", "2025-06-30", 11.0, "10-Q", "2025-07-25"),
        fact("2025-01-01", "2025-09-30", 18.0, "10-Q", "2025-10-25"),
        fact("2025-01-01", "2025-12-31", 26.0, "10-K", "2026-02-01"),
        fact("2026-01-01", "2026-03-31", 7.0, "10-Q", "2026-04-25"),
        fact("2026-01-01", "2026-06-30", 15.0, "10-Q", "2026-07-25"),
    ]
    eps = [
        fact("2025-01-01", "2025-03-31", 1.0, "10-Q", "2025-04-25"),
        fact("2025-04-01", "2025-06-30", 1.1, "10-Q", "2025-07-25"),
        fact("2025-07-01", "2025-09-30", 1.2, "10-Q", "2025-10-25"),
        fact("2025-01-01", "2025-09-30", 3.3, "10-Q", "2025-10-25"),
        fact("2025-01-01", "2025-12-31", 4.6, "10-K", "2026-02-01"),
        fact("2026-01-01", "2026-03-31", 1.4, "10-Q", "2026-04-25"),
        fact("2026-04-01", "2026-06-30", 1.5, "10-Q", "2026-07-25"),
    ]
    ni = [
        fact("2025-01-01", "2025-03-31", 20.0, "10-Q", "2025-04-25"),
        fact("2025-04-01", "2025-06-30", 22.0, "10-Q", "2025-07-25"),
        fact("2025-07-01", "2025-09-30", 24.0, "10-Q", "2025-10-25"),
        fact("2025-01-01", "2025-09-30", 66.0, "10-Q", "2025-10-25"),
        fact("2025-01-01", "2025-12-31", 92.0, "10-K", "2026-02-01"),
        fact("2026-01-01", "2026-03-31", 28.0, "10-Q", "2026-04-25"),
        fact("2026-04-01", "2026-06-30", 30.0, "10-Q", "2026-07-25"),
        fact("2026-04-01", "2026-06-30", 999.0, "8-K", "2026-07-24"),    # not a 10-Q/10-K: ignored
    ]
    inst = lambda vals, filed="2026-07-25": [fact(None, e, v, "10-Q", filed) for e, v in vals]
    return {
        "cik": 1234567, "entityName": "Example Corp",
        "facts": {"us-gaap": {
            "SalesRevenueNet": {"units": {"USD": rev_old}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rev}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": ocf}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": capex}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": eps}},
            "NetIncomeLoss": {"units": {"USD": ni}},
            "OperatingIncomeLoss": {"units": {"USD": [fact("2026-04-01", "2026-06-30", 40.0, "10-Q", "2026-07-25")]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": inst([("2025-12-31", 50.0), ("2026-06-30", 60.0)])}},
            "ShortTermInvestments": {"units": {"USD": inst([("2026-06-30", 15.0)])}},
            "LongTermDebtNoncurrent": {"units": {"USD": inst([("2026-06-30", 80.0)])}},
            "LongTermDebtCurrent": {"units": {"USD": inst([("2026-06-30", 10.0)])}},
            "CommercialPaper": {"units": {"USD": inst([("2026-06-30", 5.0)])}},
            "StockholdersEquity": {"units": {"USD": inst([("2025-12-31", 300.0), ("2026-06-30", 320.0)])}},
        }},
    }
