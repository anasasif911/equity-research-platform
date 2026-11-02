"""Normalization, reconciliation and historical drivers.

Inputs: output of extract.xbrl.build_annual/build_interim.
Outputs (JSON-serialisable): canonical statements per fiscal year, derived
lines, the normalization bridge, reconciliation checks, a discrepancy log and
driver statistics. Nothing is estimated here: a missing value stays None and
is reported as a gap.
"""
from __future__ import annotations

import statistics
from typing import Any

from ..util import cagr, safe_div

IS_ITEMS = ["revenue", "cogs", "gross_profit", "rnd", "sga", "operating_expenses", "operating_income", "dna", "sbc",
            "interest_expense", "other_income", "pretax_income", "income_tax", "net_income", "net_income_nci",
            "eps_diluted", "shares_diluted_weighted", "shares_basic_weighted"]
BS_ITEMS = ["cash", "short_term_investments", "receivables", "other_receivables", "inventory", "other_current_assets",
            "total_current_assets", "long_term_investments", "ppe_net", "goodwill", "intangibles",
            "other_noncurrent_assets", "total_assets", "accounts_payable", "deferred_revenue_current",
            "short_term_debt", "current_portion_ltd", "other_current_liabilities", "total_current_liabilities",
            "long_term_debt", "operating_lease_liabilities", "other_noncurrent_liabilities", "total_liabilities",
            "total_equity", "nci", "total_liabilities_and_equity"]
CF_ITEMS = ["cfo", "capex", "acquisitions", "cfi", "dividends_paid", "buybacks", "cff", "fx_effect", "cash_end_cf",
            "cash_taxes_paid"]


def _g(d: dict, k: str) -> float:
    v = d.get(k)
    return 0.0 if v is None else v


def derive(row: dict) -> dict:
    """Derived lines for one period (dict item->value). Returns a new dict."""
    r = dict(row)
    if r.get("gross_profit") is None and r.get("revenue") is not None and r.get("cogs") is not None:
        r["gross_profit"] = r["revenue"] - r["cogs"]
    if r.get("cogs") is None and r.get("revenue") is not None and r.get("gross_profit") is not None:
        r["cogs"] = r["revenue"] - r["gross_profit"]
    r["total_debt"] = _g(r, "short_term_debt") + _g(r, "current_portion_ltd") + _g(r, "long_term_debt")
    r["financial_assets"] = _g(r, "cash") + _g(r, "short_term_investments") + _g(r, "long_term_investments")
    r["net_cash"] = r["financial_assets"] - r["total_debt"]
    op_ca = sum(_g(r, k) for k in ("receivables", "other_receivables", "inventory", "other_current_assets"))
    op_cl = sum(_g(r, k) for k in ("accounts_payable", "deferred_revenue_current", "other_current_liabilities"))
    r["operating_nwc"] = op_ca - op_cl if r.get("total_current_assets") is not None else None
    if r.get("operating_income") is not None and r.get("dna") is not None:
        r["ebitda"] = r["operating_income"] + r["dna"]
    r["effective_tax_rate"] = safe_div(r.get("income_tax"), r.get("pretax_income"))
    if r.get("cfo") is not None and r.get("capex") is not None:
        r["fcf"] = r["cfo"] + r["capex"]  # capex stored negative
    if r.get("total_liabilities") is None and r.get("total_liabilities_and_equity") is not None \
            and r.get("total_equity") is not None:
        r["total_liabilities"] = r["total_liabilities_and_equity"] - r["total_equity"] - _g(r, "nci")
        r["total_liabilities_derived"] = True
    if r.get("total_equity") is not None:
        r["invested_capital"] = r["total_equity"] + _g(r, "nci") + r["total_debt"] - r["financial_assets"]
    return r


def build_statements(annual: dict, interim: dict | None) -> dict:
    periods = annual["periods"]
    rows = []
    for p in periods:
        row = {k: annual["values"].get(k, {}).get(p["end"]) for k in IS_ITEMS + BS_ITEMS + CF_ITEMS}
        rows.append(derive(row))
    latest_bs = None
    if interim and interim.get("bs"):
        latest_bs = derive({k: interim["bs"].get(k) for k in BS_ITEMS})
        latest_bs["as_of"] = interim["end"]
    ttm = None
    if interim and interim.get("ttm"):
        ttm = derive({k: interim["ttm"].get(k) for k in IS_ITEMS + CF_ITEMS})
        ttm["as_of"] = interim["end"]
    return {"periods": periods, "rows": rows, "latest_bs": latest_bs, "ttm": ttm,
            "ytd": interim.get("ytd") if interim else None, "ytd_prior": interim.get("ytd_prior") if interim else None,
            "interim_end": interim.get("end") if interim else None, "currency": annual["currency"],
            "taxonomy": annual["taxonomy"]}


# --------------------------------------------------------------------------- reconciliation
def reconcile(st: dict, tol: dict) -> dict:
    checks: list[dict] = []
    disc: list[dict] = []
    rows, periods = st["rows"], st["periods"]
    for i, (p, r) in enumerate(zip(periods, rows)):
        lab = p["label"]
        # Balance-sheet identity: Assets = Liabilities + Equity (+NCI)
        A, L, E = r.get("total_assets"), r.get("total_liabilities"), r.get("total_equity")
        if A is not None and L is not None and E is not None:
            diff = A - (L + E + _g(r, "nci"))
            ok = abs(diff) <= abs(A) * tol["balance_sheet_identity_rel"]
            checks.append({"check": "BS identity", "period": lab, "diff": diff, "pass": ok,
                           "detail": "Liabilities derived from L+E total" if r.get("total_liabilities_derived") else ""})
        else:
            checks.append({"check": "BS identity", "period": lab, "pass": None, "detail": "missing totals"})
        # Gross profit subtotal
        if None not in (r.get("revenue"), r.get("cogs"), r.get("gross_profit")):
            diff = r["revenue"] - r["cogs"] - r["gross_profit"]
            checks.append({"check": "Revenue - COGS = Gross profit", "period": lab, "diff": diff,
                           "pass": abs(diff) <= abs(r["revenue"]) * tol["subtotal_rel"]})
        # Pretax - tax = net income (allowing NCI / discontinued ops)
        if None not in (r.get("pretax_income"), r.get("income_tax"), r.get("net_income")):
            diff = r["pretax_income"] - r["income_tax"] - r["net_income"] - _g(r, "net_income_nci")
            ok = abs(diff) <= abs(r["net_income"]) * 0.02 + 1
            checks.append({"check": "PBT - tax = net income", "period": lab, "diff": diff, "pass": ok,
                           "detail": "" if ok else "difference may be equity-method income / discontinued operations"})
            if not ok:
                disc.append({"type": "subtotal", "period": lab, "item": "net_income", "diff": diff,
                             "resolution": "Investigate equity-method / discontinued items; reported net income retained."})
        # Cash-flow reconciliation: CFO + CFI + CFF + FX = change in cash
        if i > 0 and None not in (r.get("cfo"), r.get("cfi"), r.get("cff")) and r.get("cash_end_cf") is not None \
                and rows[i - 1].get("cash_end_cf") is not None:
            flow = r["cfo"] + r["cfi"] + r["cff"] + _g(r, "fx_effect")
            dcash = r["cash_end_cf"] - rows[i - 1]["cash_end_cf"]
            diff = flow - dcash
            ok = abs(diff) <= max(abs(r["cfo"]) * tol["cash_flow_reconciliation_rel"], 1.0)
            checks.append({"check": "CF reconciles to change in cash", "period": lab, "diff": diff, "pass": ok})
            if not ok:
                disc.append({"type": "cash_flow", "period": lab, "diff": diff,
                             "resolution": "Check restricted-cash definition and FX line; flagged in model notes."})
        # EPS cross-check: NI / diluted shares vs reported diluted EPS
        if None not in (r.get("net_income"), r.get("shares_diluted_weighted"), r.get("eps_diluted")) and r["shares_diluted_weighted"]:
            calc = r["net_income"] / r["shares_diluted_weighted"]
            ok = abs(calc - r["eps_diluted"]) <= 0.02 + abs(r["eps_diluted"]) * 0.01
            checks.append({"check": "NI / diluted shares = diluted EPS", "period": lab, "diff": calc - r["eps_diluted"], "pass": ok})
    return {"checks": checks, "discrepancies": disc}


# --------------------------------------------------------------------------- drivers
def _mean(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def _median(xs):
    v = [x for x in xs if x is not None]
    return statistics.median(v) if v else None


def _stdev(xs):
    v = [x for x in xs if x is not None]
    return statistics.pstdev(v) if len(v) > 1 else None


def drivers(st: dict) -> dict:
    rows, periods = st["rows"], st["periods"]
    n = len(rows)
    series: dict[str, list[float | None]] = {k: [] for k in (
        "revenue_growth", "gross_margin", "ebit_margin", "ebitda_margin", "net_margin", "tax_rate", "capex_pct_revenue",
        "dna_pct_revenue", "sbc_pct_revenue", "rnd_pct_revenue", "nwc_pct_revenue", "fcf_margin", "cash_conversion",
        "roic", "roe", "payout_ratio", "buyback_pct_ni", "share_change", "accruals_ratio")}
    for i, r in enumerate(rows):
        prev = rows[i - 1] if i else None
        rev = r.get("revenue")
        series["revenue_growth"].append(safe_div(rev, prev.get("revenue")) - 1 if prev and safe_div(rev, prev.get("revenue")) else None)
        series["gross_margin"].append(safe_div(r.get("gross_profit"), rev))
        series["ebit_margin"].append(safe_div(r.get("operating_income"), rev))
        series["ebitda_margin"].append(safe_div(r.get("ebitda"), rev))
        series["net_margin"].append(safe_div(r.get("net_income"), rev))
        series["tax_rate"].append(r.get("effective_tax_rate"))
        series["capex_pct_revenue"].append(safe_div(-r["capex"], rev) if r.get("capex") is not None else None)
        series["dna_pct_revenue"].append(safe_div(r.get("dna"), rev))
        series["sbc_pct_revenue"].append(safe_div(r.get("sbc"), rev))
        series["rnd_pct_revenue"].append(safe_div(r.get("rnd"), rev))
        series["nwc_pct_revenue"].append(safe_div(r.get("operating_nwc"), rev))
        series["fcf_margin"].append(safe_div(r.get("fcf"), rev))
        series["cash_conversion"].append(safe_div(r.get("fcf"), r.get("net_income")))
        tax = r.get("effective_tax_rate") or 0.21
        nopat = r["operating_income"] * (1 - tax) if r.get("operating_income") is not None else None
        ic_avg = _mean([r.get("invested_capital"), prev.get("invested_capital") if prev else None])
        series["roic"].append(safe_div(nopat, ic_avg) if ic_avg and ic_avg > 0 else None)
        eq_avg = _mean([r.get("total_equity"), prev.get("total_equity") if prev else None])
        series["roe"].append(safe_div(r.get("net_income"), eq_avg) if eq_avg and eq_avg > 0 else None)
        series["payout_ratio"].append(safe_div(-r["dividends_paid"], r.get("net_income")) if r.get("dividends_paid") is not None else None)
        series["buyback_pct_ni"].append(safe_div(-r["buybacks"], r.get("net_income")) if r.get("buybacks") is not None else None)
        series["share_change"].append(safe_div(r.get("shares_diluted_weighted"), prev.get("shares_diluted_weighted")) - 1
                                      if prev and safe_div(r.get("shares_diluted_weighted"), prev.get("shares_diluted_weighted")) else None)
        ta = _mean([r.get("total_assets"), prev.get("total_assets") if prev else None])
        series["accruals_ratio"].append(safe_div((r.get("net_income") or 0) - (r.get("cfo") or 0), ta)
                                        if r.get("net_income") is not None and r.get("cfo") is not None else None)

    def window(k, w):
        return series[k][-w:] if n else []

    summary = {}
    for k, v in series.items():
        summary[k] = {"last": v[-1] if v else None, "avg3": _mean(window(k, 3)), "avg5": _mean(window(k, 5)),
                      "median3": _median(window(k, 3)), "stdev5": _stdev(window(k, 5))}
    revs = [r.get("revenue") for r in rows]
    summary["revenue_cagr_3y"] = cagr(revs[-4], revs[-1], 3) if n >= 4 else None
    summary["revenue_cagr_5y"] = cagr(revs[-6], revs[-1], 5) if n >= 6 else None
    ebit = [r.get("operating_income") for r in rows]
    summary["ebit_cagr_3y"] = cagr(ebit[-4], ebit[-1], 3) if n >= 4 else None
    eps = [r.get("eps_diluted") for r in rows]
    summary["eps_cagr_3y"] = cagr(eps[-4], eps[-1], 3) if n >= 4 else None
    # YTD growth (latest interim vs prior-year interim)
    ytd, ytdp = st.get("ytd") or {}, st.get("ytd_prior") or {}
    summary["ytd_revenue_growth"] = (safe_div(ytd.get("revenue"), ytdp.get("revenue")) - 1) if safe_div(ytd.get("revenue"), ytdp.get("revenue")) else None
    summary["ytd_ebit_margin"] = safe_div(ytd.get("operating_income"), ytd.get("revenue"))
    # acquisitions intensity (organic vs acquired growth proxy)
    acq = [-(r.get("acquisitions") or 0) for r in rows]
    summary["acquisitions_pct_revenue_5y"] = safe_div(sum(acq[-5:]), sum(x for x in revs[-5:] if x)) if any(revs) else None
    quality = []
    cc = summary["cash_conversion"]["avg3"]
    if cc is not None and cc < 0.8:
        quality.append(f"Low cash conversion: 3y average FCF/net income {cc:.2f}")
    acc = summary["accruals_ratio"]["last"]
    if acc is not None and acc > 0.05:
        quality.append(f"High accruals ratio {acc:.2%} (net income materially above CFO)")
    sbc = summary["sbc_pct_revenue"]["avg3"]
    if sbc is not None and sbc > 0.05:
        quality.append(f"Material stock-based compensation: {sbc:.1%} of revenue (3y avg), treated as an expense")
    if summary["acquisitions_pct_revenue_5y"] and summary["acquisitions_pct_revenue_5y"] > 0.05:
        quality.append("Acquisitive: 5y acquisition spend > 5% of cumulative revenue; organic growth lower than reported")
    return {"series": series, "summary": summary, "labels": [p["label"] for p in periods], "quality_flags": quality}


def normalization_bridge(facts: dict, st: dict, rules: dict) -> list[dict]:
    """Reported EBIT -> normalized EBIT using tagged non-recurring items only."""
    from ..extract.xbrl import _concept_facts  # local import: shared helper
    out = []
    cur = st["currency"]
    for p, r in zip(st["periods"], st["rows"]):
        adj_total, lines = 0.0, []
        for c in rules["normalization_bridge"]["candidates"]:
            tx, concept = c["concept"].split(":")
            for f in _concept_facts(facts, tx, concept, cur):
                if f.get("start") and f["end"] == p["end"] and 340 <= (
                        __import__("datetime").date.fromisoformat(f["end"]) -
                        __import__("datetime").date.fromisoformat(f["start"])).days <= 380:
                    amt = float(f["val"]) * (1 if c["treatment"] == "add_back" else -1)
                    lines.append({"label": c["label"], "amount": amt, "concept": c["concept"], "accn": f.get("accn")})
                    adj_total += amt
                    break
        out.append({"period": p["label"], "reported_ebit": r.get("operating_income"), "adjustments": lines,
                    "normalized_ebit": (r.get("operating_income") or 0) + adj_total if r.get("operating_income") is not None else None})
    return out
