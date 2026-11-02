"""Mandatory checks and quality gates (methodology/quality_gates.yaml).

Status vocabulary: VERIFIED, QUALIFIED (passed with a disclosed limitation),
FAILED, NOT_RUN. A gate that could not be executed is never shown as passed.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path

import pdfplumber
from openpyxl import load_workbook

from ..model.sheetmodel import FIRST_DATA_COL, evaluate
from ..util import fmt_pct, log, parse_date, utcnow, write_json, read_json
from ..render.pdf import money


def soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def recalc_with_libreoffice(xlsx: Path, outdir: Path) -> Path | None:
    exe = soffice()
    if not exe:
        return None
    outdir = outdir.resolve()
    xlsx = xlsx.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    profile = outdir / "lo_profile"
    cmd = [exe, f"-env:UserInstallation=file://{profile}", "--headless", "--calc", "--convert-to", "xlsx:Calc MS Excel 2007 XML",
           "--outdir", str(outdir), str(xlsx)]
    subprocess.run(cmd, capture_output=True, timeout=240)
    out = outdir / xlsx.name
    return out if out.exists() else None


def export_pdf_with_libreoffice(xlsx: Path, outdir: Path) -> Path | None:
    exe = soffice()
    if not exe:
        return None
    outdir, xlsx = outdir.resolve(), xlsx.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    profile = outdir / "lo_profile"
    subprocess.run([exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(xlsx)],
                   capture_output=True, timeout=240)
    p = outdir / (xlsx.stem + ".pdf")
    return p if p.exists() else None


def render_pngs(pdf: Path, outdir: Path, prefix: str, first: int = 1, last: int | None = None, dpi: int = 70) -> list[str]:
    if not shutil.which("pdftoppm"):
        return []
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = ["pdftoppm", "-png", "-r", str(dpi), "-f", str(first)] + (["-l", str(last)] if last else []) + [str(pdf), str(outdir / prefix)]
    subprocess.run(cmd, capture_output=True, timeout=240)
    return sorted(str(p) for p in outdir.glob(f"{prefix}*.png"))


def compare_workbook(spec, engine: dict, recalced: Path) -> dict:
    wb = load_workbook(recalced, data_only=True)
    mism, errors, compared = [], [], 0

    def close(a, b):
        if isinstance(a, dt.datetime):
            a = (a - dt.datetime(1899, 12, 30)).total_seconds() / 86400
        if a is None or b is None:
            return a == b or (a in (None, 0) and b in (None, 0))
        if isinstance(a, str) or isinstance(b, str):
            return False
        return abs(a - b) <= max(1e-6, 5e-4 * max(abs(a), abs(b)))
    for r in spec.rows:
        if r.style == "header":
            continue
        ws = wb[r.sheet]
        rr = spec.row_pos[(r.sheet, r.key)]
        for c in range(spec.H + spec.N):
            src = r.fc if c >= spec.H else r.hist
            if src is None:
                continue
            v = ws.cell(row=rr, column=FIRST_DATA_COL + c).value
            e = engine["rows"][(r.sheet, r.key)][c]
            if isinstance(v, str) and (v.startswith("#") or v.startswith("Err")):
                errors.append(f"{r.sheet}!{spec.col_letter(c)}{rr} {v}")
                continue
            compared += 1
            if not close(v, e):
                mism.append({"cell": f"{r.sheet}!{spec.col_letter(c)}{rr}", "row": r.key, "workbook": v, "engine": e})
    for s in spec.scalars:
        sh, cell = spec.scalar_pos[s.name]
        v = wb[sh][cell].value
        e = engine["scalars"][s.name]
        if isinstance(v, str) and (v.startswith("#") or v.startswith("Err")):
            errors.append(f"{sh}!{cell} {v}")
            continue
        compared += 1
        if not close(v, e):
            mism.append({"cell": f"{sh}!{cell}", "row": s.name, "workbook": v, "engine": e})
    # any other error strings anywhere in the workbook (non-spec sheets: comps, checks)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and re.match(r"^(#(REF|DIV/0|VALUE|NAME|N/A|NUM|NULL)|Err:)", cell.value):
                    errors.append(f"{ws.title}!{cell.coordinate} {cell.value}")
    checks_overall = None
    if "Checks" in wb.sheetnames:
        for row in wb["Checks"].iter_rows(min_col=1, max_col=5):
            if row[0].value == "Overall model integrity":
                checks_overall = row[4].value
    return {"compared": compared, "mismatches": mism, "errors": sorted(set(errors)), "checks_overall": checks_overall,
            "dashboard": {r[0].value: r[1].value for r in wb["Dashboard"].iter_rows(min_row=6, max_row=17, max_col=2) if r[0].value}}


def pdf_text(p: Path) -> tuple[str, int]:
    with pdfplumber.open(p) as pdf:
        return "\n".join((pg.extract_text() or "") for pg in pdf.pages), len(pdf.pages)


def _g(gid, status, detail, M):
    meta = next(g for g in M.gates["gates"] if g["id"] == gid)
    return {"id": gid, "name": meta["name"], "severity": meta["severity"], "status": status, "detail": detail}


def compute_label(gates: list[dict]) -> str:
    if any(g["severity"] == "critical" and g["status"] in ("FAILED", "NOT_RUN") for g in gates):
        return "BLOCKED_PARTIAL"
    if any(g["severity"] == "major" and g["status"] in ("FAILED", "NOT_RUN") for g in gates):
        return "DRAFT_QUALIFIED"
    return "FINAL_VALIDATED"


def evaluate_gates(pl, wb_result: dict | None, report_check: dict | None, visual: dict | None) -> list[dict]:
    st, M = pl.state, pl.M
    sec, stm, val = st.get("security"), st.get("statements"), st.get("valuation")
    rec, mi, syn = st.get("reconciliation"), st.get("market_raw"), st.get("synthesis")
    crit = st.get("critique") or {}
    fev = st.get("retrieved")["companyfacts_evidence_id"]
    synthetic = pl.store.get(fev).synthetic
    cutoff = dt.datetime.fromisoformat(st.data["cutoff"]).date()
    pol = M.source_policy["freshness_days"]
    G = []
    G.append(_g("G01", "VERIFIED" if (sec.get("cik") or sec.get("identifier")) else "FAILED",
                f"{sec.get('identifier')} via {sec.get('resolution_source', 'exchange/regulator listing')} [{sec.get('evidence_id')}]; "
                f"exchange {sec.get('exchange')}", M))
    ex = st.get("extracted")
    n_vals = sum(len(v) for v in ex["annual"]["prov"].values())
    n_prov = sum(1 for v in ex["annual"]["prov"].values() for p in v.values() if p.get("accn"))
    if synthetic:
        G.append(_g("G02", "FAILED", "SYNTHETIC fixture data: not traceable to a real filing (test run)", M))
    else:
        G.append(_g("G02", "VERIFIED" if n_vals == n_prov and n_vals else "FAILED",
                    f"{n_prov}/{n_vals} historical values carry document-level provenance ({sec.get('fin_source', '')}) [{fev}]", M))
    ny = len(stm["periods"])
    G.append(_g("G03", "VERIFIED" if ny >= 5 else ("QUALIFIED" if ny >= 3 else "FAILED"), f"{ny} fiscal years of annual history", M))

    def checks(name):
        cs = [c for c in rec["checks"] if c["check"] == name]
        return [c for c in cs if c["pass"] is True], [c for c in cs if c["pass"] is False], [c for c in cs if c["pass"] is None]
    ok, bad, na = checks("BS identity")
    G.append(_g("G04", "FAILED" if bad else ("VERIFIED" if ok and not na else "QUALIFIED"),
                f"{len(ok)} pass, {len(bad)} fail, {len(na)} not testable (missing totals)", M))
    ok, bad, na = checks("CF reconciles to change in cash")
    G.append(_g("G05", "FAILED" if bad else ("VERIFIED" if ok else "NOT_RUN"), f"{len(ok)} pass, {len(bad)} fail", M))
    sub = [c for c in rec["checks"] if c["check"] in ("Revenue - COGS = Gross profit", "PBT - tax = net income", "NI / diluted shares = diluted EPS")]
    sb = [c for c in sub if c["pass"] is False]
    G.append(_g("G06", "VERIFIED" if sub and not sb else ("QUALIFIED" if sub else "NOT_RUN"),
                f"{len(sub) - len(sb)} pass, {len(sb)} fail (differences logged)", M))
    chk = val["checks"]
    wbchk = wb_result or {}
    G.append(_g("G07", "VERIFIED" if abs(chk["chk_bs"]) < 0.5 and wbchk.get("checks_overall") in ("PASS", None) else "FAILED",
                f"max |net assets - capital| = {chk['chk_bs']:.6f}m (engine); workbook checks: {wbchk.get('checks_overall')}", M))
    so = ex["shares_outstanding"]
    G.append(_g("G08", "VERIFIED" if abs(chk["chk_bridge"]) < 0.01 and so else ("QUALIFIED" if abs(chk["chk_bridge"]) < 0.01 else "FAILED"),
                "EV + cash - debt - NCI - leases = equity; per-share x shares = equity; share count from cover page "
                + (f"{so['value']:,.0f} as of {so['as_of']}" if so else "unavailable (weighted diluted used)"), M))
    # freshness / sourcing
    stale, missing = [], []
    for k in ("price", "rf", "erp", "beta"):
        if k not in mi:
            missing.append(k)
    pt = parse_date(str(mi["price"]["time"])[:10]) if "price" in mi else None
    if pt and (cutoff - pt).days > pol["market_price"]:
        stale.append(f"price {pt} ({(cutoff - pt).days}d)")
    rft = parse_date(mi.get("rf", {}).get("as_of"))
    if rft and (cutoff - rft).days > pol["risk_free_rate"]:
        stale.append(f"risk-free {rft}")
    et = parse_date(mi.get("erp", {}).get("as_of"))
    if et and (cutoff - et).days > pol["equity_risk_premium"]:
        stale.append(f"ERP {et}")
    ct = parse_date((mi.get("crp") or {}).get("as_of"))
    if ct and (cutoff - ct).days > pol["equity_risk_premium"]:
        stale.append(f"country risk premium {ct} ({(cutoff - ct).days}d; source updates semi-annually)")
    if not et and "erp" in mi:
        stale.append("ERP date not stated in source")
    fy_end = parse_date(stm["periods"][-1]["end"])
    if (cutoff - fy_end).days > pol["latest_annual_filing"]:
        stale.append(f"latest annual period {fy_end}")
    ids = [mi[k].get("evidence_id") for k in ("price", "rf", "erp") if k in mi]
    G.append(_g("G09", "FAILED" if missing or synthetic else ("QUALIFIED" if stale else "VERIFIED"),
                ("missing: " + ", ".join(missing) + "; " if missing else "") + ("synthetic fixture inputs; " if synthetic else "")
                + f"sourced {', '.join(i for i in ids if i)}; stale: {', '.join(stale) or 'none'}", M))
    d = val["dcf_base"]
    issues = []
    if d["tv_share"] > M.valuation["dcf_policy"]["tv_share_warning"]:
        issues.append(f"TV share {fmt_pct(d['tv_share'])} > 80%")
    if d["terminal_g"] > mi["rf"]["value"]:
        issues.append("g > rf")
    rng = M.gates["tolerances"].get("implied_tv_ebitda_range", {})
    lo_m, hi_m = rng.get(stm["currency"], rng.get("default", [3, 40]))
    if not (lo_m <= (d["implied_tv_ebitda"] or 0) <= hi_m):
        issues.append(f"implied terminal EV/EBITDA {d['implied_tv_ebitda']:.1f}x outside {lo_m}-{hi_m}x ({stm['currency']} range)")
    if d["ronic"] < d["wacc"] - 1e-9:
        issues.append("RONIC < WACC")
    G.append(_g("G10", "VERIFIED" if not issues else ("QUALIFIED" if all("TV share" in i for i in issues) else "FAILED"),
                "; ".join(issues) or f"TV share {fmt_pct(d['tv_share'])}, g {fmt_pct(d['terminal_g'])} < rf {fmt_pct(mi['rf']['value'])}, "
                                     f"terminal EV/EBITDA {d['implied_tv_ebitda']:.1f}x (range {lo_m}-{hi_m}x), RONIC {fmt_pct(d['ronic'])} >= WACC {fmt_pct(d['wacc'])}", M))
    sc = val["scenarios"]
    s_ok = chk["chk_sens_wacc"] > 0 and chk["chk_sens_g"] >= 0 and sc["bear"]["value_per_share"] <= sc["base"]["value_per_share"] <= sc["bull"]["value_per_share"]
    mg = val["margin_growth_grid"]["values"]
    mono = all(mg[i][2] <= mg[i + 1][2] for i in range(4)) and all(mg[2][j] <= mg[2][j + 1] for j in range(4))
    G.append(_g("G11", "VERIFIED" if s_ok and mono else "FAILED",
                f"value falls with WACC ({chk['chk_sens_wacc']:+.2f}), rises with g ({chk['chk_sens_g']:+.2f}); bear<=base<=bull; "
                f"margin/growth grid monotonic: {mono}", M))
    if wb_result is None:
        G.append(_g("G12", "NOT_RUN", "no compatible calculation engine (LibreOffice) available - workbook recalculation NOT tested", M))
        G.append(_g("G13", "NOT_RUN", "requires recalculation", M))
    else:
        mm = wb_result["mismatches"]
        G.append(_g("G12", "VERIFIED" if not mm and wb_result["compared"] else "FAILED",
                    f"LibreOffice recalculated {wb_result['compared']} formula/input cells; {len(mm)} mismatches vs engine"
                    + (f" e.g. {mm[:3]}" if mm else ""), M))
        G.append(_g("G13", "VERIFIED" if not wb_result["errors"] else "FAILED",
                    f"{len(wb_result['errors'])} error cells" + (f": {wb_result['errors'][:5]}" if wb_result["errors"] else ""), M))
    if report_check is None:
        G.append(_g("G14", "NOT_RUN", "report not yet checked", M))
    else:
        G.append(_g("G14", "VERIFIED" if not report_check["missing"] and report_check["workbook_match"] else "FAILED",
                    f"{report_check['found']}/{report_check['expected']} key figures found in report/summary text; workbook dashboard "
                    f"matches engine: {report_check['workbook_match']}" + (f"; missing {report_check['missing']}" if report_check["missing"] else ""), M))
    cited = set(re.findall(r"EV-\d{4}", str({k: v for k, v in syn.items() if not k.startswith('_')})))
    unknown = cited - set(pl.store.items)
    no_time = [e.id for e in pl.store.items.values() if not e.retrieved_at]
    G.append(_g("G15", "FAILED" if unknown or no_time else "VERIFIED",
                f"{len(pl.store.items)} sources registered; {len(cited)} distinct ids cited in narrative; unknown ids: {sorted(unknown) or 'none'}", M))
    G.append(_g("G16", "QUALIFIED" if stale else "VERIFIED", "stale: " + (", ".join(stale) or "none"), M))
    if visual is None:
        G.append(_g("G17", "NOT_RUN", "renders not produced", M))
    else:
        reviewed = visual.get("review")
        if reviewed and reviewed.get("verdict") == "pass":
            stt = "VERIFIED"
        elif reviewed and reviewed.get("verdict") == "fail":
            stt = "FAILED"
        else:
            stt = "QUALIFIED" if visual["layout_ok"] else "FAILED"
        G.append(_g("G17", stt, f"tables within frame: {visual['layout_ok']}; summary pages {visual['summary_pages']} (<=2: "
                                f"{visual['summary_pages'] <= 2}); {visual['n_png']} page images rendered for review; "
                                + (f"reviewed by {reviewed.get('reviewer')}: {reviewed.get('notes', '')[:200]}" if reviewed else
                                   "visual review record not yet provided (work/visual_review.json)"), M))
    det = crit.get("post_revision") if crit.get("post_revision") is not None else crit.get("deterministic", [])
    unresolved_crit = [f for f in (det + crit.get("model", [])) if f["severity"] == "critical"]
    G.append(_g("G18", "FAILED" if unresolved_crit else ("VERIFIED" if crit.get("model_ran") else "QUALIFIED"),
                f"deterministic critique {len(crit.get('deterministic', []))} findings; model critique "
                f"{'ran: ' + str(len(crit.get('model', []))) + ' findings' if crit.get('model_ran') else 'not run'}; unresolved critical: {len(unresolved_crit)}", M))
    prov = syn.get("_provider")
    G.append(_g("G19", "NOT_RUN" if prov in ("none", None) else "VERIFIED",
                "no inference model configured: qualitative sections not synthesised" if prov in ("none", None)
                else f"synthesis by provider '{prov}'" + (f", revised {syn.get('_revision')}x" if syn.get("_revision") else ""), M))
    return G


def check_reports(paths: dict, D_keys: dict, wb_result: dict | None) -> dict:
    txt_r, _ = pdf_text(Path(paths["report_pdf"]))
    txt_s, n_s = pdf_text(Path(paths["summary_pdf"]))
    md = Path(paths["summary_md"]).read_text()
    k = D_keys
    expected = {"price": money(k["price"]), "dcf_value": money(k["dcf_value"]), "wacc": fmt_pct(k["wacc"]),
                "tv_share": fmt_pct(k["tv_share"])}
    if k.get("fair_value") is not None:
        expected["fair_value"] = money(k["fair_value"], 0)
        expected["target_12m"] = money(k["target_12m"], 0)
    missing = [f"{n}={v} (report)" for n, v in expected.items() if v not in txt_r]
    missing += [f"{n}={v} (summary pdf)" for n, v in expected.items() if n in ("price", "fair_value", "dcf_value") and v not in txt_s]
    missing += [f"{n}={v} (summary md)" for n, v in expected.items() if n in ("price", "fair_value", "dcf_value") and v not in md]
    wb_match = None
    if wb_result:
        dash = wb_result["dashboard"]
        pairs = [("Reference price", k["price"]), ("Intrinsic value per share - DCF (live scenario)", k["dcf_value"]),
                 ("WACC", k["wacc"]), ("Enterprise value (m)", k["ev"])]
        wb_match = all(dash.get(lbl) is not None and abs(dash[lbl] - v) <= max(1e-6, 1e-6 * abs(v)) for lbl, v in pairs)
    return {"expected": len(expected) + 6, "found": len(expected) + 6 - len(missing), "missing": missing,
            "workbook_match": wb_match, "summary_pages": n_s}


def run_validation(pl):
    from ..render import render_all
    st = pl.state
    info = st.get("render_info")
    paths = info["paths"]
    vdir = st.run_dir / "validation"
    vdir.mkdir(exist_ok=True)
    ctx = pl.model_context()
    spec = ctx.spec(1)
    engine = evaluate(spec)
    rec_path = recalc_with_libreoffice(Path(paths["model_xlsx"]), vdir / "recalc")
    wb_result = compare_workbook(spec, engine, rec_path) if rec_path else None
    gates = evaluate_gates(pl, wb_result, None, None)
    st.put("gates", gates)
    st.data["label"] = compute_label(gates) + " (pending report checks)"
    # re-render with gate results, then check the final documents
    render_all(pl, final=True)
    info = st.get("render_info")
    rc = check_reports(paths, info["key_numbers"], wb_result)
    pngs = render_pngs(Path(paths["report_pdf"]), vdir / "png", "report") + \
        render_pngs(Path(paths["summary_pdf"]), vdir / "png", "summary")
    xl_pdf = export_pdf_with_libreoffice(Path(rec_path or paths["model_xlsx"]), vdir / "xlsx_pdf")
    if xl_pdf:
        pngs += render_pngs(xl_pdf, vdir / "png", "workbook", 1, 6)
    review = read_json(st.work / "visual_review.json")
    visual = {"layout_ok": all(x["fits"] for x in info["layout_log"]), "summary_pages": rc["summary_pages"], "n_png": len(pngs),
              "review": review, "pngs": pngs}
    gates = evaluate_gates(pl, wb_result, rc, visual)
    st.put("gates", gates)
    label = compute_label(gates)
    st.data["label"] = label
    st.save()
    render_all(pl, final=True)       # final documents carry the final gate table and label
    rc2 = check_reports(paths, st.get("render_info")["key_numbers"], wb_result)
    if rc2["missing"] != rc["missing"]:
        gates = evaluate_gates(pl, wb_result, rc2, visual)
        st.put("gates", gates)
    write_json(Path(paths["validation"]), {
        "label": st.data["label"], "generated_at": utcnow().isoformat(), "gates": gates,
        "workbook_recalculation": {k: v for k, v in (wb_result or {}).items() if k != "dashboard"} if wb_result else
        "NOT RUN: no LibreOffice", "report_checks": rc2, "layout_log": st.get("render_info")["layout_log"],
        "page_images": pngs, "recalculated_workbook": str(rec_path) if rec_path else None})
    log.info("validation label: %s", st.data["label"])
