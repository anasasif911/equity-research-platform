import copy

from eqr.extract import xbrl
from eqr.model import financials as F


def test_annual_periods_and_currency(built):
    ann = built["ann"]
    assert [p["label"] for p in ann["periods"]] == ["FY2020", "FY2021", "FY2022", "FY2023", "FY2024", "FY2025"]
    assert ann["currency"] == "USD" and ann["taxonomy"] == "us-gaap"


def test_restatement_uses_latest_filed_and_is_logged(built):
    ann = built["ann"]
    r = [x for x in ann["restatements"] if x["item"] == "revenue"]
    assert len(r) == 1
    assert r[0]["restated"] < r[0]["original"]            # original was 0.2% higher
    end = r[0]["period_end"]
    assert ann["values"]["revenue"][end] == r[0]["restated"]
    assert ann["prov"]["revenue"][end]["accn"] == "0009999999-24-000011"


def test_every_value_has_provenance(built):
    ann = built["ann"]
    for item, vals in ann["values"].items():
        for end in vals:
            p = ann["prov"][item][end]
            assert p["accn"] and p["concept"] and p["form"] and p["filed"]


def test_outflows_are_negative(built):
    st = built["st"]
    for r in st["rows"]:
        assert r["capex"] < 0 and r["dividends_paid"] < 0 and r["buybacks"] < 0


def test_ttm_construction(built):
    ann, inter = built["ann"], built["inter"]
    fy = ann["values"]["revenue"][ann["periods"][-1]["end"]]
    assert inter["end"] == "2026-06-27"
    assert abs(inter["ttm"]["revenue"] - (fy + inter["ytd"]["revenue"] - inter["ytd_prior"]["revenue"])) < 1
    assert inter["ttm"]["revenue"] > fy                        # YTD grew 7%


def test_shares_outstanding_cover_page(M, facts):
    so = xbrl.shares_outstanding(facts, M.normalization)
    assert so["concept"] == "dei:EntityCommonStockSharesOutstanding" and so["as_of"] == "2026-07-17"


def test_reconciliation_passes_on_clean_data(M, built):
    rec = F.reconcile(built["st"], M.gates["tolerances"])
    assert all(c["pass"] in (True, None) for c in rec["checks"])
    assert sum(1 for c in rec["checks"] if c["check"] == "BS identity" and c["pass"]) == 6
    assert sum(1 for c in rec["checks"] if c["check"] == "CF reconciles to change in cash" and c["pass"]) == 5


def test_reconciliation_detects_corruption(M, built):
    st = copy.deepcopy(built["st"])
    st["rows"][3]["total_assets"] *= 1.01
    st["rows"][4]["cfo"] += 5e9
    rec = F.reconcile(st, M.gates["tolerances"])
    fails = {(c["check"], c["period"]) for c in rec["checks"] if c["pass"] is False}
    assert ("BS identity", "FY2023") in fails
    assert ("CF reconciles to change in cash", "FY2024") in fails
    assert rec["discrepancies"]


def test_derived_lines(built):
    r = built["st"]["rows"][-1]
    assert abs(r["financial_assets"] - (r["cash"] + r["short_term_investments"] + r["long_term_investments"])) < 1
    assert abs(r["total_debt"] - (r["short_term_debt"] + r["current_portion_ltd"] + r["long_term_debt"])) < 1
    assert abs(r["fcf"] - (r["cfo"] + r["capex"])) < 1


def test_drivers(built):
    s = built["drv"]["summary"]
    assert abs(s["revenue_growth"]["last"] - 0.064) < 1e-3
    assert abs(s["ebit_margin"]["avg3"] - (1 - 0.58 - 0.075 - 0.068)) < 1e-6
    assert abs(s["ytd_revenue_growth"] - 0.07) < 1e-6
