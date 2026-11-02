import pytest

from eqr.model.sheetmodel import EvalError, ModelSpec, Row, Scalar, evaluate
from eqr.valuation import engine as V
from eqr.valuation.wacc import cost_of_capital, regression_beta


def test_formula_language_python_and_excel_agree_on_structure():
    spec = ModelSpec(hist_labels=["H1"], fc_labels=["F1", "F2"], sheets=["A", "B"])
    spec.add(Row("A", "rev", "Revenue", hist=[100.0], fc="{r:rev@-1}*(1+{s:g})"),
             Row("B", "cost", "Cost", hist="{r:A.rev}*0.5", fc="{r:A.rev}*0.5"))
    spec.scalar(Scalar("A", "g", "growth", 0.1), Scalar("B", "total", "sum", "SUM({range:A.rev})"))
    spec.layout()
    r = evaluate(spec)
    assert r["rows"][("A", "rev")] == pytest.approx([100, 110, 121])
    assert r["scalars"]["total"] == pytest.approx(231)
    assert spec.to_excel("{r:rev@-1}*(1+{s:g})", "A", 1) == "=C7*(1+$C$10)"
    assert spec.to_excel("{r:A.rev}*0.5", "B", 2) == "=A!E7*0.5"
    assert spec.to_excel("SUM({range:A.rev})", "B", None) == "=SUM(A!D7:E7)"


def test_row_order_dependency_is_detected():
    spec = ModelSpec(hist_labels=["H1"], fc_labels=["F1"], sheets=["A"])
    spec.add(Row("A", "x", "x", fc="{r:y}+1"), Row("A", "y", "y", fc=[1.0]))
    spec.layout()
    with pytest.raises(EvalError):
        evaluate(spec)


def test_integrated_model_identities(ctx):
    r = ctx.evaluate(1)
    S = r["scalars"]
    assert not r["errors"]
    assert max(abs(x) for x in r["rows"][("BS", "check")]) < 1e-6          # forecast BS balances every year
    assert abs(S["chk_hist_bs"]) < 1e-6
    assert abs(S["sens_2_2"] - S["value_per_share"]) < 1e-9                  # sensitivity centre = DCF
    assert abs(S["value_per_share"] * S["diluted_shares"] - S["equity_value"]) < 1e-6
    assert S["equity_value"] == pytest.approx(S["ev"] + S["fin_assets_latest"] - S["debt_latest"] - S["nci_latest"] - S["lease_adj"])
    assert S["ev"] == pytest.approx(S["sum_pv"] + S["pv_tv"])
    # cash roll-forward: closing = opening + net change
    fa, nc = r["rows"][("BS", "fin_assets")], r["rows"][("CF", "net_change")]
    H = 6
    for k in range(H, len(fa)):
        assert fa[k] == pytest.approx(fa[k - 1] + nc[k])


def test_cash_flows_before_balance_sheet_date_are_excluded(ctx):
    r = ctx.evaluate(1)
    frac = r["rows"][("DCF", "frac")][6:]
    # FY2026 ends 2026-09-27; balance sheet 2026-06-27 -> about one quarter counted
    assert 0.2 < frac[0] < 0.3 and all(f == 1 for f in frac[1:])


def test_sensitivity_directions(ctx):
    S = ctx.evaluate(1)["scalars"]
    assert S["sens_0_2"] > S["sens_2_2"] > S["sens_4_2"]                     # higher WACC -> lower value
    assert S["sens_2_4"] >= S["sens_2_0"]                                     # RONIC >= WACC -> g non-negative effect


def test_scenarios_ordered(ctx):
    sc = V.scenario_results(ctx)
    assert sc["bear"]["value_per_share"] < sc["base"]["value_per_share"] < sc["bull"]["value_per_share"]
    assert sc["stress"]["value_per_share"] < sc["bear"]["value_per_share"]


def test_reverse_dcf_reproduces_price(ctx):
    rd = V.reverse_dcf(ctx, 100.0)
    assert rd["growth_shift"] is not None
    v = ctx.evaluate(1, V._shifted(ctx.kw["scenarios"], growth_shift=rd["growth_shift"]))["scalars"]["value_per_share"]
    assert v == pytest.approx(100.0, abs=0.01)


def test_margin_growth_grid_monotonic(ctx):
    g = V.margin_growth_grid(ctx)["values"]
    assert all(g[i][2] < g[i + 1][2] for i in range(4))


def test_comps():
    peers = [dict(ticker=t, price=p, shares=10, fin_assets=5, debt=10, nci=0, ttm_revenue=100, ttm_ebitda=e, ttm_net_income=n)
             for t, p, e, n in (("A", 50, 40, 20), ("B", 60, 45, 25), ("C", 40, 30, 15), ("D", 80, 50, 30))]
    target = dict(ticker="T", price=45, shares=10, fin_assets=20, debt=0, ttm_revenue=100, ttm_ebitda=40, ttm_net_income=20)
    c = V.comps(target, peers)
    evs = sorted((p * 10 + 10 - 5) / e for _, p, e, _ in (("A", 50, 40, 20), ("B", 60, 45, 25), ("C", 40, 30, 15), ("D", 80, 50, 30)))
    assert c["medians"]["ev_ebitda"] == pytest.approx((evs[1] + evs[2]) / 2)
    assert c["implied"]["ev_ebitda"] == pytest.approx((c["medians"]["ev_ebitda"] * 40 + 20) / 10)
    assert c["n_valid"] == 4


def test_comps_requires_three_peers():
    c = V.comps(dict(price=1, shares=1, fin_assets=0, debt=0, ttm_ebitda=1, ttm_net_income=1),
                [dict(ticker="A", price=1, shares=1, fin_assets=0, debt=0, ttm_revenue=1, ttm_ebitda=1, ttm_net_income=1)])
    assert c["medians"]["ev_ebitda"] is None and c["central"] is None


def test_ddm_and_residual_income():
    d = V.ddm([1.0] * 5, 0.10, 0.0, years_offset=1.0)
    assert d["value"] == pytest.approx(10.0)                                   # perpetuity of 1 at 10%
    ri = V.residual_income(100.0, 0.10, 0.10, 0.02, 0.5, 5, 10.0, roe_fade_to=0.10)
    assert ri["equity_value"] == pytest.approx(100.0)                           # ROE = ke -> value = book


def test_method_selection_rules(M):
    avail = {k: True for k in ("historical_financials", "market_price", "risk_free_rate", "equity_risk_premium", "beta",
                               "dividend_history", "book_value", "roe_history")}
    bank = {m["id"]: m for m in V.select_methods(M.valuation, "bank", avail, {})}
    assert bank["fcff_dcf"]["status"] == "rejected" and "unsuitable" in bank["fcff_dcf"]["reason"]
    assert bank["residual_income"]["status"] == "selected"
    ind = {m["id"]: m for m in V.select_methods(M.valuation, "mature_cash_generator", {**avail, "payout_ok": False}, {})}
    assert ind["fcff_dcf"]["status"] == "selected" and ind["ddm"]["status"] == "rejected"
    assert ind["trading_comps"]["status"] == "rejected"                         # no peer data flag
    assert ind["reverse_dcf"]["status"] == "selected" and ind["reverse_dcf"]["weight"] == 0


def test_rating_withheld_when_confidence_low():
    assert V.rating(0.5, 0.09, "LOW", False)["rating"] == "NO RATING"
    assert V.rating(0.5, 0.09, "HIGH", True)["rating"] == "NO RATING"
    assert V.rating(0.25, 0.09, "MEDIUM", False)["rating"] == "BUY"
    assert V.rating(0.05, 0.09, "MEDIUM", False)["rating"] == "HOLD"


def test_regression_beta_recovers_known_beta():
    import json
    from pathlib import Path
    from eqr.sources.market import parse_yahoo
    fx = Path(__file__).parent / "fixtures" / "synthetic"
    s = parse_yahoo((fx / "prices_syn.json").read_bytes())["series"]
    b = parse_yahoo((fx / "prices_spy.json").read_bytes())["series"]
    r = regression_beta(s, b)
    assert r["n"] == 60 and abs(r["raw"] - 1.15) < 3 * r["se"]
    assert r["adjusted"] == pytest.approx(0.67 * r["raw"] + 0.33)


def test_cost_of_capital():
    c = cost_of_capital(rf=0.04, erp=0.05, beta_adj=1.2, kd_pre=0.05, tax=0.2, equity_value=900, debt=100)
    assert c["ke"] == pytest.approx(0.10) and c["wacc"] == pytest.approx(0.9 * 0.10 + 0.1 * 0.04)
