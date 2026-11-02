"""Pipeline stages for Pakistan Stock Exchange listings (market = psx)."""
from __future__ import annotations

import io
import re
from pathlib import Path

from ..config import load_yaml, METHODOLOGY_DIR
from ..extract import psx_financials as X
from ..extract import pdf_tables as T
from ..job import NEEDS_INPUT, AWAITING_EVIDENCE, Paused
from ..sources import damodaran, psx, rates
from ..sources.evidence import SourceUnavailable
from ..util import log, utcnow, write_json, read_json

PROFILE = METHODOLOGY_DIR / "markets" / "psx.yaml"


def profile() -> dict:
    return load_yaml(PROFILE)


def resolve(pl) -> None:
    sym = re.sub(r"^PSX:", "", pl.state.data["ticker_input"].upper()).split(":")[0]
    try:
        info, ev = psx.company(pl.store, sym)
    except ValueError:
        raise Paused(NEEDS_INPUT, f"'{sym}' was not found on the PSX data portal. Check the symbol (e.g. SAZEW, LUCK, MEBL).", {})
    anns = info.pop("announcements")
    vendor = info.pop("vendor_financials")
    prof = profile()
    fye = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6, "July": 7, "August": 8, "September": 9,
           "October": 10, "November": 11, "December": 12}.get(info.get("fiscal_year_end", "June"), 6)
    sec_info = {
        "market": "psx", "status": "resolved", "ticker": sym, "name": info["name"], "exchange": "PSX",
        "identifier": f"PSX: {sym}", "classification": info["sector"].title(), "sector": info["sector"],
        "evidence_id": ev, "resolution_source": "PSX data portal company page", "country": "PK", "currency": "PKR",
        "fiscal_year_end_month": fye, "fiscal_year_end": info.get("fiscal_year_end"), "profile": info,
        "fin_source": "issuer annual/quarterly reports filed on PSX (PDF)",
        "fin_source_long": ("issuer annual report (audited statements; six-year summary for earlier years) and latest quarterly report, "
                            "downloaded from the PSX data portal; PDF tables parsed by column position, OCR artefacts normalised, "
                            "conflicts between documents resolved by accounting identities and logged"),
        "coc_method": ("PKR CAPM: GoP Ijarah Sukuk yield (PSX auction) minus Pakistan default spread, plus bottom-up beta x mature ERP, "
                       "plus Pakistan country risk premium (Damodaran); market-value weights"),
        "peer_source": "PSX data portal quotes (exchange) and summary earnings (Capital Stake, tier 4)",
    }
    pl.state.put("security", sec_info)
    pl.state.put("psx_announcements", anns)
    pl.state.put("psx_vendor", {"financials": vendor, "evidence_id": ev})


def playbook_id(sec_info: dict) -> str:
    return profile()["sector_playbooks"].get(sec_info["sector"].upper(), "general")


def _pdf_text_pages(data: bytes) -> list[str]:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [(p.extract_text() or "") for p in pdf.pages]


def _topic_excerpt(pages: list[str], patterns: list[str], max_pages: int, max_chars: int) -> tuple[str, list[int]]:
    hits = []
    for i, t in enumerate(pages):
        u = re.sub(r"\s+", " ", t.upper())
        if any(re.search(p, u) for p in patterns):
            hits.append(i)
    hits = hits[:max_pages]
    text = "\n\n".join(f"[page {i + 1}]\n{pages[i]}" for i in hits)
    return text[:max_chars], [i + 1 for i in hits]


def retrieve(pl) -> None:
    st, store = pl.state, pl.store
    sec_info = st.get("security")
    prof = profile()
    anns = st.get("psx_announcements")
    docs = psx.pick_documents(anns, prof["documents"])
    if "annual_report" not in docs:
        raise Paused(AWAITING_EVIDENCE, "No annual report found in the PSX announcements list for this company. Place the latest "
                                        "annual report PDF in the evidence inbox and resume.", {"requests": store.blocked_requests()})
    raw_dir = st.run_dir / "sources" / "docs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    got = {}
    ar_bytes, ar_ev = psx.document(store, docs["annual_report"], "annual_report", "Annual report")
    (raw_dir / "annual_report.pdf").write_bytes(ar_bytes)
    got["annual_report"] = {"evidence_id": ar_ev, "date": docs["annual_report"]["date"], "title": docs["annual_report"]["title"],
                            "path": "sources/docs/annual_report.pdf"}
    if "quarterly_report" in docs and docs["quarterly_report"]["date"] > docs["annual_report"]["date"]:
        q_bytes, q_ev = psx.document(store, docs["quarterly_report"], "quarterly_report", "Quarterly report")
        (raw_dir / "quarterly_report.pdf").write_bytes(q_bytes)
        got["quarterly_report"] = {"evidence_id": q_ev, "date": docs["quarterly_report"]["date"],
                                   "title": docs["quarterly_report"]["title"], "path": "sources/docs/quarterly_report.pdf"}
    # narrative evidence
    excerpts = []
    ar_pages = _pdf_text_pages(ar_bytes)
    ar_ev_obj = store.get(ar_ev)
    topics = [
        ("Directors' report - business and operations review", [r"DIRECTORS'? REPORT", r"OPERATING (PERFORMANCE|RESULTS)"], 6),
        ("Chairman's review / outlook", [r"CHAIRMAN'?S? (REVIEW|MESSAGE)", r"FUTURE OUTLOOK", r"FORWARD[- ]LOOKING STATEMENT"], 4),
        ("Business model, market share and competition", [r"BUSINESS MODEL", r"MARKET SHARE", r"COMPETITIVE (LANDSCAPE|POSITION)", r"SWOT"], 4),
        ("Principal risks and opportunities", [r"RISKS? AND OPPORTUNIT", r"PRINCIPAL RISKS", r"RISK MANAGEMENT"], 4),
        ("Governance, related parties and capital structure", [r"PATTERN OF SHAREHOLDING", r"RELATED PART(Y|IES)", r"COMPOSITION OF (THE )?BOARD"], 3),
        ("Segment information and contingencies", [r"SEGMENT (INFORMATION|REPORTING)", r"CONTINGENCIES AND COMMITMENTS"], 3),
    ]
    for title, pats, maxp in topics:
        text, pages = _topic_excerpt(ar_pages, pats, maxp, 18000)
        if text:
            excerpts.append(pl._excerpt(ar_ev_obj, f"Annual report - {title} (pp. {', '.join(map(str, pages))})", text, limit=18000))
    if "quarterly_report" in got:
        qp = _pdf_text_pages((raw_dir / "quarterly_report.pdf").read_bytes())
        text, pages = _topic_excerpt(qp, [r"DIRECTORS'? REVIEW", r"FUTURE OUTLOOK", r"COMPANY'?S FINANCIAL PERFORMANCE"], 3, 16000)
        if text:
            excerpts.append(pl._excerpt(store.get(got["quarterly_report"]["evidence_id"]),
                                        f"Quarterly report - directors' review (pp. {', '.join(map(str, pages))})", text, limit=16000))
    for kind, label, maxn in (("briefing", "Corporate briefing", 1), ("results", "Financial results announcement", 1)):
        if kind in docs:
            try:
                b, e = psx.document(store, docs[kind], kind, label)
                t = "\n".join(_pdf_text_pages(b))
                if len(t.strip()) > 200:
                    excerpts.append(pl._excerpt(store.get(e), f"{label}: {docs[kind]['title']} ({docs[kind]['date']})", t, limit=12000))
            except (SourceUnavailable, Exception) as ex:  # noqa: BLE001 - narrative docs are optional
                log.info("optional document %s skipped: %s", kind, ex)
    for a in (docs.get("all_operating_data") or [])[:3] + (docs.get("all_material_information") or [])[:3]:
        try:
            b, e = psx.document(store, a, "disclosure", "Company disclosure")
            t = "\n".join(_pdf_text_pages(b))
            if len(t.strip()) > 100:
                excerpts.append(pl._excerpt(store.get(e), f"PSX disclosure: {a['title']} ({a['date']})", t, limit=6000))
        except Exception as ex:  # noqa: BLE001
            log.info("disclosure skipped: %s", ex)
    st.put("excerpts", excerpts)
    # tax rates stated in the annual report
    full = re.sub(r"\s+", " ", "\n".join(ar_pages))
    m = re.search(r"corporat\w* tax rate of (\d{1,2})%.{0,60}?super tax at (\d{1,2})%", full, re.I) or \
        re.search(r"corporate tax at (\d{1,2})%.{0,40}?super tax at the rate of (\d{1,2})%", full, re.I)
    tax = None
    if m:
        page = next((i + 1 for i, t in enumerate(ar_pages) if m.group(1) + "%" in t and "super tax" in t.lower()), None)
        tax = {"statutory": (int(m.group(1)) + int(m.group(2))) / 100, "corporate": int(m.group(1)) / 100,
               "super_tax": int(m.group(2)) / 100, "page": page, "evidence_id": ar_ev, "text": m.group(0)[:200]}
    st.put("tax_disclosure", tax)
    # market inputs
    mi = {}
    pinfo = sec_info["profile"]
    mi["price"] = {"value": pinfo["price"], "time": pinfo.get("price_time"), "currency": "PKR", "evidence_id": sec_info["evidence_id"],
                   "source": "PSX data portal quote"}
    cutoff = pl.cutoff.date()
    gis = psx.gis_risk_free(store, cutoff, prof["cost_of_capital"]["min_tenor_years"])
    crp = damodaran.country_risk(store, "Pakistan")
    erp = rates.equity_risk_premium(store)
    pb = st.get("plan")["playbook"]
    ind = pb.get("damodaran_industry")
    beta = damodaran.industry_beta(store, ind) if ind else None
    if gis and crp:
        rf_adj = gis["gov_yield"] - crp["default_spread"]
        mi["rf"] = {"value": rf_adj, "as_of": gis["as_of"], "evidence_id": gis["evidence_id"], "tenor": gis["instrument"],
                    "gov_yield": gis["gov_yield"], "default_spread": crp["default_spread"], "short_rate": gis["short_yield"],
                    "label": "Risk-free rate (PKR, default-spread adjusted)",
                    "source_text": (f"{gis['instrument']} cut-off yield {gis['gov_yield']:.2%} (PSX GIS auction {gis['as_of']}) "
                                    f"[{gis['evidence_id']}] less Pakistan default spread {crp['default_spread']:.2%} "
                                    f"({crp['rating']}, Damodaran {crp['as_of']}) [{crp['evidence_id']}]")}
        mi["crp"] = {"value": crp["crp"], "rating": crp["rating"], "as_of": crp["as_of"], "evidence_id": crp["evidence_id"],
                     "source_text": f"Pakistan CRP, Damodaran ctryprem ({crp['as_of']}; Moody's {crp['rating']}) [{crp['evidence_id']}]",
                     "lambda": prof["cost_of_capital"]["lambda"]}
    if erp:
        mi["erp"] = {"value": erp["value"], "as_of": erp.get("as_of"), "evidence_id": erp["evidence_id"], "context": erp.get("context", "")[:300]}
    if beta:
        mi["beta_inputs"] = beta
    st.put("market_raw", mi)
    st.put("retrieved", {"documents": got, "doc_list": {k: v for k, v in docs.items() if isinstance(v, dict)},
                         "peers": _peers(pl, pb, sec_info), "blocked": [r["id"] for r in store.blocked_requests()],
                         "companyfacts_evidence_id": ar_ev,
                         "access_note": prof["access_notes"]})
    missing = [k for k in ("price", "rf", "erp", "crp") if k not in mi] + ([] if beta else ["beta_inputs"])
    if missing:
        raise Paused(AWAITING_EVIDENCE, f"Critical market inputs unavailable: {', '.join(missing)}.",
                     {"missing": missing, "requests": store.blocked_requests(critical_only=True)})


def _peers(pl, pb: dict, sec_info: dict) -> list[dict]:
    out = []
    for p in [x for x in (pl.state.data["options"].get("peers") or pb.get("peers_hint") or []) if x != sec_info["ticker"]][:pl.cfg["budget"]["max_peers"]]:
        try:
            info, ev = psx.company(pl.store, p)
        except (SourceUnavailable, ValueError):
            continue
        vend = info.get("vendor_financials", {})
        qcols = [k for k in vend if k.startswith("Q")]
        ttm_ni = sum(vend[k].get("Profit after Taxation") or 0 for k in qcols[:4]) * 1000 if len(qcols) >= 4 else None
        if not info.get("shares") or not info.get("price"):
            continue
        pt = (info.get("price_time") or "")[:10]
        if not pt or (pl.cutoff.date() - __import__("datetime").date.fromisoformat(pt)).days > pl.M.source_policy["freshness_days"]["peer_market_data"]:
            log.info("peer %s excluded: stale quote %s", p, pt)
            pl.state.event("retrieve", f"peer {p} excluded from comparables: last PSX quote {pt or 'n/a'} is stale")
            continue
        out.append({"ticker": p, "name": info["name"], "price": info["price"], "price_time": info.get("price_time"),
                    "shares": info["shares"], "fin_assets": 0.0, "debt": 0.0, "nci": 0.0, "ttm_revenue": None, "ttm_ebitda": None,
                    "ttm_ebit": None, "ttm_net_income": ttm_ni, "pe_ttm_portal": info.get("pe_ttm"),
                    "ttm_basis": "sum of last 4 quarters (PSX portal summary, Capital Stake)", "evidence_ids": [ev],
                    "sector": info.get("sector")})
    return out


def extract(pl) -> None:
    st = pl.state
    sec_info = st.get("security")
    got = st.get("retrieved")["documents"]
    rules = load_yaml(METHODOLOGY_DIR / "normalization_pk.yaml")
    logl, disc = [], []
    ar = got["annual_report"]
    cands = X.extract_annual_report(str(st.run_dir / ar["path"]), ar["evidence_id"], ar["date"], ar["title"], rules,
                                    sec_info["fiscal_year_end_month"], logl)
    if not cands:
        raise Paused(AWAITING_EVIDENCE, "Financial statements could not be located in the annual report PDF (possibly a scanned "
                                        "image without text). OCR is not available in this environment.", {})
    years = sorted({fy for (_, fy) in cands})
    chosen = X.resolve(cands, years, disc)
    annual = X.to_canonical(chosen, sec_info["fiscal_year_end_month"], "PKR")
    interim = None
    if "quarterly_report" in got:
        q = X.extract_quarterly(str(st.run_dir / got["quarterly_report"]["path"]), got["quarterly_report"]["evidence_id"],
                                got["quarterly_report"]["date"], got["quarterly_report"]["title"], rules, logl)
        interim = X.interim_canonical(q, annual, "PKR")
        if interim and q.get("bs_prior"):
            # cross-check: quarterly comparative balance sheet vs audited year-end
            last = annual["periods"][-1]["end"]
            for k in ("total_assets", "total_equity", "cash"):
                a = annual["values"].get(k, {}).get(last)
                b = q["bs_prior"].get(k, {}).get("value")
                if a is not None and b is not None and abs(a - b) > 2:
                    disc.append({"type": "cross_document", "period": annual["periods"][-1]["label"], "item": k, "diff": b - a,
                                 "resolution": "Quarterly report comparative differs from the audited annual figure; audited figure used."})
    vendor = st.get("psx_vendor")
    vchecks = X.cross_check_vendor(annual, vendor["financials"], disc, vendor["evidence_id"])
    pinfo = sec_info["profile"]
    so = {"value": pinfo["shares"], "as_of": (pinfo.get("price_time") or "")[:10], "concept": "PSX equity profile: Shares",
          "accn": sec_info["evidence_id"], "form": "PSX data portal", "filed": None}
    st.put("extracted", {"annual": annual, "interim": interim, "shares_outstanding": so, "dps": {}, "filing_evidence": {},
                         "extraction_log": logl, "source_discrepancies": disc, "vendor_checks": vchecks,
                         "extension_namespaces": []})


def model_market(pl, st_: dict, drv: dict, base: dict) -> tuple[dict, dict]:
    """market_kw and coc for build_spec (PSX)."""
    stt = pl.state
    mi, ex = stt.get("market_raw"), stt.get("extracted")
    import datetime as dt
    bs = st_["latest_bs"] or st_["rows"][-1]
    bs_date = dt.date.fromisoformat(st_["interim_end"]) if st_.get("latest_bs") else dt.date.fromisoformat(st_["periods"][-1]["end"])
    so = ex["shares_outstanding"]
    fev = stt.get("retrieved")["companyfacts_evidence_id"]
    qev = (stt.get("retrieved")["documents"].get("quarterly_report") or {}).get("evidence_id", fev)
    tax = stt.get("tax_disclosure") or {}
    statutory = tax.get("statutory") or base["statutory_tax"]
    debt = bs.get("total_debt") or 0
    equity_mv = mi["price"]["value"] * so["value"]
    bi = mi["beta_inputs"]
    de = debt / equity_mv if equity_mv else 0.0
    from ..sources.damodaran import relever
    beta_l = relever(bi["unlevered_beta_cash_corrected"], de, statutory)
    mi["beta"] = {"value": beta_l, "adjusted": beta_l, "label": "Beta (bottom-up, re-levered)", "method": "bottom-up",
                  "evidence_ids": [bi["evidence_id"]],
                  "description": (f"Damodaran EM '{bi['industry']}' unlevered beta (cash-corrected) {bi['unlevered_beta_cash_corrected']:.2f} "
                                  f"({bi['n_firms']} firms, {bi['as_of']}) re-levered at market D/E {de:.1%} and tax {statutory:.0%} "
                                  f"[{bi['evidence_id']}]. No Blume adjustment (bottom-up).")}
    stt.put("market_raw", mi)
    market_kw = {
        "price": mi["price"]["value"], "price_source": f"{mi['price']['source']} {mi['price']['time']} [{mi['price']['evidence_id']}]",
        "diluted_shares": so["value"],
        "shares_source": f"PSX equity profile: {so['value']:,.0f} shares [{so['accn']}]; no dilutive instruments (annual report note on EPS)",
        "debt": debt, "fin_assets": bs.get("financial_assets") or 0, "nci": bs.get("nci") or 0, "lease_adj": 0.0,
        "lease_note": "IFRS 16 lease liabilities: none material reported separately (included in other liabilities if any)",
        "bs_source": f"Balance sheet as of {bs_date.isoformat()} [{qev if st_.get('latest_bs') else fev}]",
        "valuation_date": pl.cutoff.date(), "bs_date": bs_date,
    }
    roic = drv["summary"]["roic"]["avg3"]
    coc = {"rf": mi["rf"]["value"], "rf_source": mi["rf"]["source_text"], "erp": mi["erp"]["value"],
           "erp_source": f"Damodaran implied ERP (mature market) {mi['erp'].get('as_of') or ''} [{mi['erp']['evidence_id']}]",
           "crp": mi["crp"]["value"] * mi["crp"]["lambda"], "crp_source": mi["crp"]["source_text"] + f"; lambda {mi['crp']['lambda']}",
           "beta_raw": beta_l, "beta_source": mi["beta"]["description"], "blume_w": 1.0,
           "kd_pre": base["drivers"]["kd"]["values"][0], "kd_source": base["drivers"]["kd"]["rationale"],
           "tax_marginal": statutory, "roic_hist": min(roic, 0.6) if roic is not None else 0.0}
    return market_kw, coc
