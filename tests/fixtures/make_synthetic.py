"""Generate the SYNTHETIC test company used by the test-suite and the offline
pipeline check. Every file produced here is fictional and labelled
SYNTHETIC; it must never be presented as real company data.

The statements are built so accounting identities hold exactly (cash is the
cash-flow roll-forward, equity rolls NI + SBC - distributions), which lets the
tests prove that reconciliation checks pass on clean data and fail on
corrupted data.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
from pathlib import Path

OUT = Path(__file__).parent / "synthetic"
FY_ENDS = ["2019-09-28", "2020-09-26", "2021-09-25", "2022-09-24", "2023-09-30", "2024-09-28", "2025-09-27"]
FILED = {e: (dt.date.fromisoformat(e) + dt.timedelta(days=35)).isoformat() for e in FY_ENDS}
CIK = 9999999


def build_years():
    years = []
    rev = 200_000e6
    prev = dict(cash=30_000e6, inv_st=40_000e6, inv_lt=100_000e6, recv=20_000e6, invent=5_000e6, oca=10_000e6,
                ppe=35_000e6, onca=30_000e6, ap=45_000e6, defrev=8_000e6, ocl=40_000e6, cp=5_000e6, ltd_c=8_000e6,
                ltd=90_000e6, oncl=45_000e6)
    prev["equity"] = (prev["cash"] + prev["inv_st"] + prev["inv_lt"] + prev["recv"] + prev["invent"] + prev["oca"] + prev["ppe"]
                      + prev["onca"]) - (prev["ap"] + prev["defrev"] + prev["ocl"] + prev["cp"] + prev["ltd_c"] + prev["ltd"] + prev["oncl"])
    shares = 16_000e6
    growth = [0.0, 0.055, 0.28, 0.08, -0.03, 0.02, 0.064]
    for i, end in enumerate(FY_ENDS):
        if i:
            rev *= 1 + growth[i]
        y = {"end": end}
        y["rev"] = rev
        y["cogs"] = rev * 0.58
        y["gp"] = rev - y["cogs"]
        y["rnd"] = rev * 0.075
        y["sga"] = rev * 0.068
        y["opinc"] = y["gp"] - y["rnd"] - y["sga"]
        y["dna"] = rev * 0.03
        y["sbc"] = rev * 0.028
        debt_prev = prev["cp"] + prev["ltd_c"] + prev["ltd"]
        y["intexp"] = debt_prev * 0.035
        y["other"] = (prev["cash"] + prev["inv_st"] + prev["inv_lt"]) * 0.025 - y["intexp"]
        y["pretax"] = y["opinc"] + y["other"]
        y["tax"] = y["pretax"] * 0.16
        y["ni"] = y["pretax"] - y["tax"]
        y["div"] = y["ni"] * 0.15
        y["bb"] = y["ni"] * 0.80
        # balance sheet (operating items by ratio)
        cur = dict(recv=rev * 0.10, invent=rev * 0.02, oca=rev * 0.05, onca=rev * 0.14, ap=rev * 0.20,
                   defrev=rev * 0.035, ocl=rev * 0.17, oncl=rev * 0.18, inv_st=rev * 0.15, inv_lt=rev * 0.45,
                   cp=5_000e6, ltd_c=9_000e6, ltd=max(60_000e6, 95_000e6 - i * 5_000e6))
        y["capex"] = rev * 0.033
        cur["ppe"] = prev["ppe"] + y["capex"] - y["dna"]
        d_opa = sum(cur[k] - prev[k] for k in ("recv", "invent", "oca", "onca"))
        d_opl = sum(cur[k] - prev[k] for k in ("ap", "defrev", "ocl", "oncl"))
        y["cfo"] = y["ni"] + y["dna"] + y["sbc"] - d_opa + d_opl
        d_inv = (cur["inv_st"] - prev["inv_st"]) + (cur["inv_lt"] - prev["inv_lt"])
        y["cfi"] = -y["capex"] - d_inv
        d_debt = (cur["cp"] + cur["ltd_c"] + cur["ltd"]) - debt_prev
        y["cff"] = -y["div"] - y["bb"] + d_debt
        y["fx"] = 0.0
        cur["cash"] = prev["cash"] + y["cfo"] + y["cfi"] + y["cff"]
        cur["equity"] = prev["equity"] + y["ni"] + y["sbc"] - y["div"] - y["bb"]
        y.update(cur)
        y["ca"] = cur["cash"] + cur["inv_st"] + cur["recv"] + cur["invent"] + cur["oca"]
        y["assets"] = y["ca"] + cur["inv_lt"] + cur["ppe"] + cur["onca"]
        y["cl"] = cur["ap"] + cur["defrev"] + cur["ocl"] + cur["cp"] + cur["ltd_c"]
        y["liab"] = y["cl"] + cur["ltd"] + cur["oncl"]
        y["le"] = y["liab"] + cur["equity"]
        assert abs(y["assets"] - y["le"]) < 1.0, (end, y["assets"] - y["le"])
        price_proxy = 150 * (1.08 ** i)
        shares -= y["bb"] / price_proxy
        y["shares_w"] = shares * 1.004
        y["shares_b"] = shares
        y["eps"] = round(y["ni"] / y["shares_w"], 2)
        y["dps"] = round(y["div"] / shares, 2)
        years.append(y)
        prev = {k: y[k] for k in prev}
    return years


def facts_json(years):
    gaap: dict = {}

    def put(concept, unit, val, end, start=None, form="10-K", fy=None, fp="FY", filed=None, accn=None):
        e = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
        if start:
            e["start"] = start
        gaap.setdefault(concept, {"label": concept, "units": {}})["units"].setdefault(unit, []).append(e)

    durations = {"RevenueFromContractWithCustomerExcludingAssessedTax": "rev", "CostOfGoodsAndServicesSold": "cogs",
                 "GrossProfit": "gp", "ResearchAndDevelopmentExpense": "rnd", "SellingGeneralAndAdministrativeExpense": "sga",
                 "OperatingIncomeLoss": "opinc", "DepreciationDepletionAndAmortization": "dna", "ShareBasedCompensation": "sbc",
                 "InterestExpense": "intexp", "NonoperatingIncomeExpense": "other",
                 "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": "pretax",
                 "IncomeTaxExpenseBenefit": "tax", "NetIncomeLoss": "ni", "NetCashProvidedByUsedInOperatingActivities": "cfo",
                 "PaymentsToAcquirePropertyPlantAndEquipment": "capex", "NetCashProvidedByUsedInInvestingActivities": "cfi",
                 "PaymentsOfDividends": "div", "PaymentsForRepurchaseOfCommonStock": "bb",
                 "NetCashProvidedByUsedInFinancingActivities": "cff"}
    instants = {"CashAndCashEquivalentsAtCarryingValue": "cash", "MarketableSecuritiesCurrent": "inv_st",
                "AccountsReceivableNetCurrent": "recv", "InventoryNet": "invent", "OtherAssetsCurrent": "oca",
                "AssetsCurrent": "ca", "MarketableSecuritiesNoncurrent": "inv_lt", "PropertyPlantAndEquipmentNet": "ppe",
                "OtherAssetsNoncurrent": "onca", "Assets": "assets", "AccountsPayableCurrent": "ap",
                "ContractWithCustomerLiabilityCurrent": "defrev", "OtherLiabilitiesCurrent": "ocl", "CommercialPaper": "cp",
                "LongTermDebtCurrent": "ltd_c", "LiabilitiesCurrent": "cl", "LongTermDebtNoncurrent": "ltd",
                "OtherLiabilitiesNoncurrent": "oncl", "Liabilities": "liab", "StockholdersEquity": "equity",
                "LiabilitiesAndStockholdersEquity": "le",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": "cash"}
    for i, y in enumerate(years):
        if i == 0:
            start = "2018-09-30"
        else:
            start = (dt.date.fromisoformat(years[i - 1]["end"]) + dt.timedelta(days=1)).isoformat()
        y["start"] = start
        fy = int(y["end"][:4])
        accn = f"0009999999-{str(fy)[2:]}-0000{i + 10}"
        for c, k in durations.items():
            if i == 0:
                continue
            put(c, "USD", round(y[k]), y["end"], start, fy=fy, filed=FILED[y["end"]], accn=accn)
        for c, k in instants.items():
            put(c, "USD", round(y[k]), y["end"], fy=fy, filed=FILED[y["end"]], accn=accn)
        if i:
            put("EarningsPerShareDiluted", "USD/shares", y["eps"], y["end"], start, fy=fy, filed=FILED[y["end"]], accn=accn)
            put("CommonStockDividendsPerShareDeclared", "USD/shares", y["dps"], y["end"], start, fy=fy, filed=FILED[y["end"]], accn=accn)
            put("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", round(y["shares_w"]), y["end"], start, fy=fy,
                filed=FILED[y["end"]], accn=accn)
            put("WeightedAverageNumberOfSharesOutstandingBasic", "shares", round(y["shares_b"]), y["end"], start, fy=fy,
                filed=FILED[y["end"]], accn=accn)
    # A restatement: FY2023 revenue originally reported 0.2% higher, superseded in the FY2024 10-K
    y23 = years[4]
    put("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", round(y23["rev"] * 1.002), y23["end"], y23["start"],
        fy=2023, filed="2023-11-01", accn="0009999999-23-000001")
    gaap["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"][-1]["filed"] = "2023-11-01"
    for e in gaap["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]:
        if e["end"] == y23["end"] and e["accn"] != "0009999999-23-000001":
            e["filed"] = "2024-11-01"
            e["accn"] = "0009999999-24-000011"
    # Interim: 9 months to 2026-06-27 (YTD) and prior-year 9 months to 2025-06-28
    last = years[-1]
    q_end, ytd_start = "2026-06-27", "2025-09-28"
    p_end, p_start = "2025-06-28", last["start"]
    for c, k in durations.items():
        put(c, "USD", round(last[k] * 0.75 * 1.07), q_end, ytd_start, form="10-Q", fy=2026, fp="Q3", filed="2026-07-31",
            accn="0009999999-26-000020")
        put(c, "USD", round(last[k] * 0.75), p_end, p_start, form="10-Q", fy=2025, fp="Q3", filed="2025-08-01",
            accn="0009999999-25-000073")
    q_bs = {k: last[k] * 1.03 for k in ("cash", "inv_st", "recv", "invent", "oca", "inv_lt", "ppe", "onca",
                                        "ap", "defrev", "ocl", "cp", "ltd_c", "ltd", "oncl")}
    q_bs["ca"] = q_bs["cash"] + q_bs["inv_st"] + q_bs["recv"] + q_bs["invent"] + q_bs["oca"]
    q_bs["assets"] = q_bs["ca"] + q_bs["inv_lt"] + q_bs["ppe"] + q_bs["onca"]
    q_bs["cl"] = q_bs["ap"] + q_bs["defrev"] + q_bs["ocl"] + q_bs["cp"] + q_bs["ltd_c"]
    q_bs["liab"] = q_bs["cl"] + q_bs["ltd"] + q_bs["oncl"]
    q_bs["equity"] = q_bs["assets"] - q_bs["liab"]
    q_bs["le"] = q_bs["assets"]
    for c, k in instants.items():
        put(c, "USD", round(q_bs[k]), q_end, form="10-Q", fy=2026, fp="Q3", filed="2026-07-31", accn="0009999999-26-000020")
    for c, v in (("WeightedAverageNumberOfDilutedSharesOutstanding", last["shares_w"] * 0.98),
                 ("WeightedAverageNumberOfSharesOutstandingBasic", last["shares_b"] * 0.98)):
        put(c, "shares", round(v), q_end, ytd_start, form="10-Q", fy=2026, fp="Q3", filed="2026-07-31", accn="0009999999-26-000020")
    dei = {"EntityCommonStockSharesOutstanding": {"label": "shares", "units": {"shares": [
        {"end": "2026-07-17", "val": round(last["shares_b"] * 0.975), "accn": "0009999999-26-000020", "fy": 2026, "fp": "Q3",
         "form": "10-Q", "filed": "2026-07-31"}]}}}
    return {"cik": CIK, "entityName": "SYNTHETIC DEMO CO (fictional)", "facts": {"dei": dei, "us-gaap": gaap}}


def prices():
    random.seed(7)
    start = dt.date(2020, 10, 1)
    stock, bench = [], []
    s, b = 100.0, 300.0
    t0 = int(dt.datetime(2020, 10, 1, tzinfo=dt.timezone.utc).timestamp())
    ts = []
    for m in range(73):
        d = dt.date(start.year + (start.month - 1 + m) // 12, (start.month - 1 + m) % 12 + 1, 1)
        rb = random.gauss(0.008, 0.045)
        rs = 0.002 + 1.15 * rb + random.gauss(0, 0.04)
        b *= 1 + rb
        s *= 1 + rs
        ts.append(int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()))
        stock.append(round(s, 4)); bench.append(round(b, 4))

    def chart(sym, closes, price):
        return {"chart": {"result": [{"meta": {"currency": "USD", "symbol": sym, "regularMarketPrice": price,
                                                "regularMarketTime": ts[-1] + 86400 * 30, "fullExchangeName": "SYNTHETIC"},
                                       "timestamp": ts, "indicators": {"quote": [{"close": closes}],
                                                                       "adjclose": [{"adjclose": closes}]}}], "error": None}}
    return chart("SYN", stock, stock[-1]), chart("SPY", bench, bench[-1])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    years = build_years()
    (OUT / "companyfacts.json").write_text(json.dumps(facts_json(years)))
    subs = {"cik": str(CIK), "name": "SYNTHETIC DEMO CO (fictional)", "sic": "3571", "sicDescription": "ELECTRONIC COMPUTERS",
            "tickers": ["SYN"], "exchanges": ["SYNTHETIC"], "fiscalYearEnd": "0927", "stateOfIncorporation": "DE",
            "filings": {"recent": {"accessionNumber": ["0009999999-26-000020", "0009999999-25-000079"],
                                   "filingDate": ["2026-07-31", "2025-10-31"], "reportDate": ["2026-06-27", "2025-09-27"],
                                   "form": ["10-Q", "10-K"], "primaryDocument": ["syn-q3.htm", "syn-10k.htm"],
                                   "items": ["", ""]}}}
    (OUT / "submissions.json").write_text(json.dumps(subs))
    (OUT / "tickers.json").write_text(json.dumps({"fields": ["cik", "name", "ticker", "exchange"],
                                                  "data": [[CIK, "SYNTHETIC DEMO CO (fictional)", "SYN", "SYNTHETIC"],
                                                           [9999998, "OTHER SYNTHETIC A", "DUP", "Nasdaq"],
                                                           [9999997, "OTHER SYNTHETIC B", "DUP", "NYSE"]]}))
    st, bm = prices()
    (OUT / "prices_syn.json").write_text(json.dumps(st))
    (OUT / "prices_spy.json").write_text(json.dumps(bm))
    rows = ["Date,1 Mo,3 Mo,1 Yr,2 Yr,5 Yr,10 Yr,30 Yr"]
    for d in ("10/30/2026", "10/29/2026", "10/28/2026"):
        rows.append(f"{d},4.10,4.05,3.90,3.80,3.95,4.20,4.60")
    (OUT / "treasury.csv").write_text("\n".join(rows) + "\n")
    (OUT / "erp.json").write_text(json.dumps({"value": 0.045, "as_of": "2026-10-01",
                                              "context": "SYNTHETIC fixture value for tests - not a market observation"}))
    (OUT / "10k.htm").write_text("<html><body><p>SYNTHETIC FIXTURE - fictional company.</p>"
                                 "<h2>Item 1. Business</h2><p>The Company designs fictional devices and sells fictional services "
                                 "to consumers worldwide.</p><h2>Item 1A. Risk Factors</h2><p>Fictional risk: component costs and "
                                 "tariffs may affect gross margin.</p><h2>Item 2. Properties</h2><p>n/a</p>"
                                 "<h2>Item 7. Management's Discussion and Analysis</h2><p>Fictional MD&A.</p><h2>Item 8.</h2></body></html>")
    print("synthetic fixtures written to", OUT)


if __name__ == "__main__":
    main()
