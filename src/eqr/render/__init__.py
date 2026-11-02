"""Artifact rendering. All numbers come from run checkpoints produced by the
engine; the workbook is generated from the same model spec, and gate G14
verifies the PDFs against the independently recalculated workbook."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..util import fmt_pct, read_json, write_json
from . import charts
from .excel import build_workbook
from .pdf import LAYOUT_LOG, build_report, build_summary, money


def deliverable_paths(run_dir: Path, ticker: str) -> dict:
    d = run_dir / "deliverables"
    d.mkdir(exist_ok=True)
    t = ticker.upper().replace(".", "-")
    return {"report_pdf": d / f"{t}_equity_research_report.pdf", "summary_pdf": d / f"{t}_investment_summary.pdf",
            "summary_md": d / f"{t}_investment_summary.md", "model_xlsx": d / f"{t}_financial_model.xlsx",
            "sources_csv": d / f"{t}_source_register.csv", "sources_json": d / f"{t}_source_register.json",
            "supporting_json": d / f"{t}_supporting_data.json", "manifest": d / "manifest.json",
            "validation": d / "validation_report.json"}


def assemble(pl) -> dict:
    st = pl.state
    sec, stm, drv = st.get("security"), st.get("statements"), st.get("drivers")
    val, syn, mi = st.get("valuation"), st.get("synthesis"), st.get("market_raw")
    ex, plan = st.get("extracted"), st.get("plan")
    base = st.get("assumptions")
    gates = st.get("gates") or []
    fev = st.get("retrieved")["companyfacts_evidence_id"]
    sources = pl.store.source_register()
    synthetic = any(s["synthetic"] for s in sources if s["id"] == fev)
    cur = stm["currency"]
    periods, rows = stm["periods"], stm["rows"]
    idx = list(range(max(0, len(rows) - 6), len(rows)))
    labels = [periods[i]["label"] for i in idx]

    def line(lbl, key, scale=1e6, dp=0, pct=False):
        out = [lbl]
        for i in idx:
            v = rows[i].get(key)
            out.append("n/a" if v is None else (fmt_pct(v) if pct else money(v / scale, dp)))
        return out
    s = drv["series"]
    hist_rows = [["", *labels], line("Revenue", "revenue"),
                 ["  growth", *[fmt_pct(s["revenue_growth"][i]) for i in idx]],
                 line("Gross profit", "gross_profit"), ["  gross margin", *[fmt_pct(s["gross_margin"][i]) for i in idx]],
                 line("Operating income (EBIT)", "operating_income"), ["  EBIT margin", *[fmt_pct(s["ebit_margin"][i]) for i in idx]],
                 line("Net income", "net_income"), line("Diluted EPS", "eps_diluted", 1, 2),
                 line("Cash from operations", "cfo"), line("Capex", "capex"), line("Free cash flow", "fcf"),
                 ["  FCF / net income", *[("n/a" if s["cash_conversion"][i] is None else f"{s['cash_conversion'][i]:.2f}x") for i in idx]],
                 ["  ROIC (NOPAT / avg IC)", *[fmt_pct(s["roic"][i]) for i in idx]],
                 line("Cash & investments", "financial_assets"), line("Gross debt", "total_debt"), line("Total equity", "total_equity")]
    capalloc = [["", *labels], line("Dividends paid", "dividends_paid"), line("Share repurchases", "buybacks"),
                line("Acquisitions", "acquisitions"), line("Capex", "capex"), line("Stock-based compensation", "sbc"),
                ["  Diluted shares (m)", *[money((rows[i].get("shares_diluted_weighted") or 0) / 1e6, 0) for i in idx]]]
    # forecast table from spec evaluation
    ctx = pl.model_context()
    spec = ctx.spec(1)
    from ..model.sheetmodel import evaluate
    ev = evaluate(spec)
    R = ev["rows"]
    H, N = spec.H, spec.N
    fl = spec.fc_labels

    def fr(lbl, key, dp=0, pct=False, per_share=False):
        vals = R[key][H:]
        return [lbl, *[(fmt_pct(v) if pct else money(v, 2 if per_share else dp)) for v in vals]]
    fc_rows = [["", *fl], fr("Revenue", ("IS", "revenue")), fr("  growth", ("IS", "growth"), pct=True),
               fr("EBIT", ("IS", "ebit")), fr("  EBIT margin", ("IS", "ebit_margin"), pct=True),
               fr("Net income", ("IS", "net_income")), fr("Diluted EPS", ("IS", "eps"), per_share=True),
               fr("FCFF", ("DCF", "fcff")), fr("Free cash flow (CFO - capex)", ("CF", "fcf")),
               fr("Cash & investments (closing)", ("BS", "fin_assets")), fr("BS check (must be 0)", ("BS", "check"))]
    a_rows = [{"label": v["label"], "first": fmt_pct(v["values"][0]) if v["fmt"] == "percent" else money(v["values"][0] / 1e6, 0),
               "last": fmt_pct(v["values"][-1]) if v["fmt"] == "percent" else money(v["values"][-1] / 1e6, 0),
               "rationale": v["rationale"]} for v in base["drivers"].values()]
    a_rows.append({"label": "Terminal growth", "first": "", "last": fmt_pct(base["terminal_growth"]), "rationale": base["terminal_rationale"]})
    d = val["dcf_base"]
    beta = mi["beta"]
    wacc_rows = [[mi["rf"].get("label", "Risk-free rate"), fmt_pct(mi["rf"]["value"], 2), mi["rf"].get("source_text", f"[{mi['rf']['evidence_id']}]")],
                 ["Equity risk premium (mature market)", fmt_pct(mi["erp"]["value"], 2), f"Damodaran implied ERP {mi['erp'].get('as_of') or ''} [{mi['erp']['evidence_id']}]"]]
    if mi.get("crp"):
        wacc_rows.append(["Country risk premium", fmt_pct(mi["crp"]["value"], 2), mi["crp"]["source_text"]])
    wacc_rows += [[beta.get("label", "Beta (raw / Blume-adjusted)"), f"{beta['value']:.2f} / {beta['adjusted']:.2f}", beta["description"]],
                 ["Cost of equity", fmt_pct(d["ke"], 2), "CAPM: rf + beta x ERP"],
                 ["After-tax cost of debt", fmt_pct(d["kd_post"], 2), base["drivers"]["kd"]["rationale"]],
                 ["Weight of equity (market)", fmt_pct(d["we"], 1), "Price x diluted shares vs book gross debt"],
                 ["WACC", fmt_pct(d["wacc"], 2), "Market-value weights; no circularity"]]
    dcf_rows = [["PV of explicit FCFF", money(d["sum_pv"], 0)], ["PV of terminal value", money(d["pv_tv"], 0)],
                ["Enterprise value", money(d["ev"], 0)], ["+ Cash & investments (latest BS)", money(d["fin_assets_latest"], 0)],
                ["- Gross debt", money(d["debt_latest"], 0)], ["- Non-controlling interests", money(d["nci_latest"], 0)],
                ["- Lease liabilities treated as debt", money(d["lease_adj"], 0)], ["Equity value", money(d["equity_value"], 0)],
                ["Diluted shares (m)", money(d["diluted_shares"], 1)], ["Intrinsic value per share (today)", money(d["value_per_share"])],
                ["DCF-only 12-month value (value x (1+ke) - dividends); headline target uses reconciled fair value", money(d["target_12m"])],
                ["Terminal value share of EV", fmt_pct(d["tv_share"])],
                ["Implied terminal EV / NTM EBITDA", f"{d['implied_tv_ebitda']:.1f}x"]]
    sens = val["sensitivity"]
    sdp = 0 if min(min(r) for r in sens["values"]) >= 100 else 2
    sens_rows = [["WACC \\ g", *[fmt_pct(g) for g in sens["g"]]]] + \
                [[fmt_pct(w), *[money(v, sdp) for v in row]] for w, row in zip(sens["wacc"], sens["values"])]
    sc = val["scenarios"]
    nice = {"revenue_growth_delta": "growth", "margin_delta": "EBIT margin", "wacc_delta": "WACC", "terminal_g_delta": "terminal g",
            "revenue_shock_years": "growth shock years"}

    def _def(dfn):
        parts = [f"{nice.get(a, a)} {b * 100:+.2f}pp" if isinstance(b, float) else f"{nice.get(a, a)}: {b}" for a, b in dfn.items()
                 if not (isinstance(b, float) and b == 0)]
        return ", ".join(parts) or "base assumptions"
    scen_rows = [[sc[k]["label"].split(" (")[0], _def(sc[k]["definition"]),
                  money(sc[k]["value_per_share"]), fmt_pct(sc[k]["upside"]), fmt_pct(sc[k]["wacc"]), fmt_pct(sc[k]["terminal_g"]),
                  fmt_pct(sc[k]["probability"], 0) if sc[k]["probability"] else "tail test"] for k in ("bull", "base", "bear", "stress")]
    rec = val["reconciled"]
    comps = val.get("comps_summary")
    comps_rows, comps_note = None, ""
    if comps and comps.get("peers"):
        comps_rows = [["Ticker", "EV/Sales", "EV/EBITDA", "P/E", "Mkt cap (bn)", "Price", "Price time"]] + \
                     [[p["ticker"], f"{p['ev_sales']:.1f}x" if p["ev_sales"] else "n/m", f"{p['ev_ebitda']:.1f}x" if p["ev_ebitda"] else "n/m",
                       f"{p['pe']:.1f}x" if p["pe"] else "n/m", money(p["market_cap"] / 1e9, 0), money(p["price"]), str(p["price_time"])[:16]]
                      for p in comps["peers"]]
        med = comps["medians"]
        mx = lambda v: f"{v:.1f}x" if v else "n/m"  # noqa: E731
        comps_note = (f"Medians: EV/EBITDA {mx(med['ev_ebitda'])}, P/E {mx(med['pe'])}. Target trades at "
                      f"EV/EBITDA {mx(comps['target_multiples']['ev_ebitda'] if med['ev_ebitda'] else None)}, "
                      f"P/E {mx(comps['target_multiples']['pe'])}. "
                      f"Peer data: {sec.get('peer_source', 'issuer filings (TTM) and timestamped prices')}.")
    comps_used = any(m["id"] == "trading_comps" and m["status"] == "selected" for m in val["methods"])
    recon_text = "No defensible valuation." if rec.get("status") != "ok" else (
        f"Fair value estimate {money(rec['fair_value'])} {cur} = " + " + ".join(
            f"{fmt_pct(c['weight'], 0)} x {c['label']} ({money(c['value'])})" for c in rec["components"]) +
        f". Valuation range {money(rec['range_low'])}-{money(rec['range_high'])} spans the bear and bull DCF cases"
        + (" and the comparables cross-check" if comps_used else "") +
        f". The 12-month target {money(rec['target_12m'])} rolls fair value forward at the cost of equity less expected "
        f"dividends; expected total return {fmt_pct(rec['expected_return'])}. Present intrinsic value and the 12-month target are distinct.")
    bars = [("DCF bear-bull", sc["bear"]["value_per_share"], sc["bull"]["value_per_share"]),
            ("DCF WACC x g sensitivity", min(min(r) for r in sens["values"]), max(max(r) for r in sens["values"])),
            ("Margin x growth grid", min(min(r) for r in val["margin_growth_grid"]["values"]), max(max(r) for r in val["margin_growth_grid"]["values"]))]
    if comps_used and comps and comps.get("implied"):
        v = list(comps["implied"].values())
        iq = (comps.get("iqr") or {}).get("pe")
        ttm_ni = (stm.get("ttm") or rows[-1]).get("net_income")
        if iq and ttm_ni and d["diluted_shares"]:
            v += [iq[0] * ttm_ni / 1e6 / d["diluted_shares"], iq[1] * ttm_ni / 1e6 / d["diluted_shares"]]
        bars.append(("Comparables (P/E interquartile)" if iq else "Comparables (implied)", min(v), max(v)))
    pv = st.run_dir / "work" / "charts"
    pv.mkdir(parents=True, exist_ok=True)
    rev_series = [rows[i]["revenue"] / 1e6 for i in idx] + R[("IS", "revenue")][H:]
    mar_series = [s["ebit_margin"][i] or 0 for i in idx] + R[("IS", "ebit_margin")][H:]
    ch = {"revenue": charts.revenue_margin(pv / "revenue.png", labels + fl, rev_series, mar_series, len(idx), cur),
          "cash": charts.cash_conversion(pv / "cash.png", labels, [rows[i].get("net_income") or 0 for i in idx],
                                         [rows[i].get("fcf") or 0 for i in idx], cur),
          "football": charts.football_field(pv / "football.png", bars, mi["price"]["value"], rec.get("fair_value"), cur),
          "bridge": charts.ev_bridge(pv / "bridge.png", d["sum_pv"], d["pv_tv"], d["fin_assets_latest"], d["debt_latest"],
                                     d["nci_latest"] + d["lease_adj"], cur)}
    rc = st.get("reconciliation")
    n_fail = sum(1 for c in rc["checks"] if c["pass"] is False)
    n_pass = sum(1 for c in rc["checks"] if c["pass"] is True)
    recon_summary = (f"{n_pass} reconciliation checks passed and {n_fail} failed across {len(periods)} fiscal years "
                     f"(balance-sheet identity, revenue-COGS-gross profit, pre-tax-tax-net income, cash-flow roll-forward, EPS). "
                     f"{len(rc['discrepancies'])} discrepancy log entries (including restatements) - see Appendix C and the workbook.")
    crit = st.get("critique") or {"deterministic": [], "model": []}
    sev = lambda L, k: sum(1 for f in L if f["severity"] == k)  # noqa: E731
    critique_summary = (f"Deterministic critics raised {sev(crit['deterministic'], 'critical')} critical, {sev(crit['deterministic'], 'major')} major and "
                        f"{sev(crit['deterministic'], 'minor')} minor findings; " +
                        (f"the independent model critic raised {len(crit['model'])} findings." if crit.get("model_ran") else
                         "no model critic ran (no inference model configured).") +
                        (f" One revision round was performed." if st.get("revisions") else "") +
                        (" Model critic findings and disposition: " + "; ".join(
                            f"[{f['severity']}] {f['target'][:60]}: {f['required_revision'][:120]}" for f in crit.get("model", []))
                         + (" - addressed in the revised synthesis." if st.get("revisions") else ".") if crit.get("model") else "") +
                        " Unresolved findings: " + ("; ".join(f"[{f['severity']}] {f['target']}: {f['problem']}" for f in
                                                             (crit.get("post_revision") or crit["deterministic"]) if f["severity"] != "minor")[:1500] or "none") + ".")
    label = st.data.get("label") or "DRAFT (validation pending)"
    conf = syn.get("confidence", "LOW")
    gates_fail_critical = any(g["severity"] == "critical" and g["status"] in ("FAILED", "NOT_RUN") for g in gates)
    from ..valuation.engine import rating as rate
    rating = rate(rec.get("expected_return"), d["ke"], conf, gates_fail_critical if gates else False)
    if syn.get("view") == "INSUFFICIENT EVIDENCE":
        rating = {"rating": "NO RATING", "why": "insufficient evidence for an investment view"}
    latest = f"{periods[-1]['label']} (ended {periods[-1]['end']})" + (f"; interim to {stm['interim_end']} (TTM computed)" if stm.get("interim_end") else "")
    sr = st.get("retrieved").get("subsequent_results")
    if sr:
        latest += f"; results announced {sr['filed']} not yet in structured filings (used qualitatively)"
    so = ex["shares_outstanding"]
    prov_rows = []
    for item, dmap in ex["annual"]["prov"].items():
        for end, pv_ in sorted(dmap.items()):
            v = ex["annual"]["values"].get(item, {}).get(end)
            prov_rows.append([item, end, (v / 1e6) if (v is not None and pv_["unit"] not in ("shares",) and "/" not in pv_["unit"]) else v,
                              pv_["concept"], pv_["accn"], pv_["form"], pv_["filed"], ex["filing_evidence"].get(pv_["accn"], fev)])
    methodology_notes = [
        "Methodology is version-controlled in methodology/ (fingerprint " + plan["methodology"]["methodology_sha256"] + "): source policy, "
        "normalization rules, valuation selection, forecasting/scenario framework, cost of capital, quality gates, critique checklists.",
        "Historical data: " + sec.get("fin_source_long", sec.get("fin_source", "issuer filings")) + ".",
        "Forecast: driver-based (growth, EBIT margin, tax, D&A, capex, NWC, payout, buybacks); every assumption labelled with rationale.",
        "DCF: FCFF, mid-period discounting from the valuation date; only cash flows after the latest balance-sheet date are counted; "
        "terminal value by value-driver formula FCFF = NOPAT x (1 - g/RONIC).",
        "Cost of capital: " + sec.get("coc_method", "CAPM with sourced risk-free rate and ERP, regression beta (Blume-adjusted); market-value weights") + ".",
        "Verification: workbook formulas recalculated in LibreOffice and compared with the engine; report numbers compared with the "
        "recalculated workbook; rendered pages inspected.",
        "Judgment tasks (synthesis, critique) use the configured inference model (" + str(syn.get("_provider")) + "). Storing methodology "
        "outside the model does not remove the need for one.",
    ]
    return {
        "security": sec, "currency": cur, "synthetic": synthetic, "as_of": st.data["cutoff"][:10], "cutoff": st.data["cutoff"],
        "price": mi["price"]["value"], "price_time": mi["price"]["time"], "price_source": f"{mi['price']['source']} [{mi['price']['evidence_id']}]",
        "label": label, "synth": syn, "rec": rec, "rating": rating, "confidence": conf, "taxonomy": stm["taxonomy"],
        "latest_period_note": latest, "horizon": plan["horizon"], "fin_ev": fev, "hist_table": {"rows": hist_rows},
        "charts": ch, "quality_flags": drv["quality_flags"], "recon_summary": recon_summary, "assumption_rows": a_rows,
        "fc_table": {"rows": fc_rows}, "methods": val["methods"], "values": val["values"], "wacc_rows": wacc_rows,
        "dcf_rows": dcf_rows, "dcf": d, "scen": sc, "tv_note": (f"Gordon growth on FCFF with value-driver reinvestment; g {fmt_pct(d['terminal_g'])}, "
                                                                 f"RONIC {fmt_pct(d['ronic'])} (reinvestment rate {fmt_pct(d['terminal_g'] / d['ronic'])}); "
                                                                 f"TV is {fmt_pct(d['tv_share'])} of EV; implied terminal EV/NTM EBITDA {d['implied_tv_ebitda']:.1f}x."),
        "sens_rows": sens_rows, "reverse": val["reverse_dcf"], "comps_rows": comps_rows if comps_used else None, "comps_note": comps_note,
        "recon_text": recon_text, "scen_rows": scen_rows, "pw_value": sc["probability_weighted_value"], "gates": gates,
        "critique_summary": critique_summary, "methodology_notes": methodology_notes, "sources": sources,
        "discrepancies": rc["discrepancies"], "capalloc_rows": capalloc,
        # workbook extras
        "scenarios": sc, "reverse_dcf": val["reverse_dcf"], "margin_growth_grid": val["margin_growth_grid"],
        "reconciled": rec, "comps": comps, "bridge": st.get("normalization_bridge"), "provenance_rows": prov_rows,
        "recon_checks": rc["checks"], "lease_note": ctx.kw["market"]["lease_note"],
        "sbc_policy": pl.M.normalization["normalization_bridge"]["sbc_policy"].strip(),
        "fye_note": f"{sec.get('fiscal_year_end')} (MMDD)", "spec": spec, "shares_note": so,
    }


def summary_markdown(D: dict) -> str:
    syn, rec, d, sc = D["synth"], D["rec"], D["dcf"], D["scen"]
    cur = D["currency"]
    L = []
    if D["synthetic"]:
        L.append("> **SYNTHETIC TEST DATA** - fictional company used to test the platform. Not a real security.\n")
    s = D["security"]
    L += [f"# {s['name']} ({s['ticker']}) - investment summary", "",
          f"*As of {D['as_of']} | price {money(D['price'])} {cur} ({D['price_source']}, {D['price_time']}) | status: {D['label']}*", "",
          "| Rating | Fair value | Range | 12m target | Expected return | Confidence |", "|---|---|---|---|---|---|",
          f"| {D['rating']['rating']} | {money(rec.get('fair_value'), 0)} | {money(rec.get('range_low'), 0)}-{money(rec.get('range_high'), 0)} | "
          f"{money(rec.get('target_12m'), 0)} | {fmt_pct(rec.get('expected_return'))} | {D['confidence']} |", "",
          f"**{syn['headline']}**", "", "## Conclusion"] + [f"- {x}" for x in syn["executive_summary"][:4]] + ["", "## Thesis"]
    L += [f"- **{p['title']}** - {p['argument']} [{', '.join(p['evidence_ids'])}]" for p in syn["thesis_pillars"][:4]] or ["- No evidence-based thesis."]
    L += ["", "## Valuation", f"- DCF base {money(d['value_per_share'])} {cur}; bear {money(sc['bear']['value_per_share'])}, bull {money(sc['bull']['value_per_share'])}",
          f"- WACC {fmt_pct(d['wacc'])}, terminal growth {fmt_pct(d['terminal_g'])}, TV {fmt_pct(d['tv_share'])} of EV",
          f"- Reverse DCF: {D['reverse']['narrative_growth']}", "", "## Catalysts"]
    L += [f"- {x['event']} ({x['timing']})" for x in syn["catalysts"][:4]] or ["- None verified from evidence."]
    L += ["", "## Largest risks"] + [f"- **{r['risk']}** - {r['transmission']}" for r in syn["risks"][:4]]
    L += ["", f"*Rating rule: {D['rating']['why']}. Full sources, model and methodology in the report and workbook.*", ""]
    return "\n".join(L)


def render_all(pl, final: bool) -> dict:
    LAYOUT_LOG.clear()
    st = pl.state
    sec = st.get("security")
    paths = deliverable_paths(st.run_dir, sec["ticker"])
    D = assemble(pl)
    cellmap = build_workbook(paths["model_xlsx"], spec=D["spec"], data=D)
    build_report(paths["report_pdf"], D)
    build_summary(paths["summary_pdf"], D)
    paths["summary_md"].write_text(summary_markdown(D))
    with paths["sources_csv"].open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(D["sources"][0].keys()))
        w.writeheader()
        w.writerows(D["sources"])
    write_json(paths["sources_json"], D["sources"])
    write_json(paths["supporting_json"], {
        "security": sec, "market_inputs": st.get("market_raw"), "statements": st.get("statements"),
        "drivers": st.get("drivers")["summary"], "assumptions": st.get("assumptions"), "scenarios_definition": st.get("scenarios"),
        "valuation": st.get("valuation"), "synthesis": st.get("synthesis"), "critique": st.get("critique"),
        "reconciliation": st.get("reconciliation"), "research_plan": st.get("plan"), "retrieval_requests": list(pl.store.requests.values())})
    st.put("render_info", {"cellmap": cellmap, "layout_log": list(LAYOUT_LOG), "final": final,
                           "key_numbers": {"fair_value": D["rec"].get("fair_value"), "dcf_value": D["dcf"]["value_per_share"],
                                           "price": D["price"], "wacc": D["dcf"]["wacc"], "target_12m": D["rec"].get("target_12m"),
                                           "ev": D["dcf"]["ev"], "tv_share": D["dcf"]["tv_share"]},
                           "rating": D["rating"], "paths": {k: str(v) for k, v in paths.items()}})
    return paths
