"""Assemble reconciled historical financials from PDF reports.

Sources, in priority order for each (item, fiscal year):
  1. audited primary statements in the latest annual report (2 years)
  2. the annual report's six-year summary (company-prepared, older years)
  3. comparative columns of the latest quarterly report (cross-check)
When candidates for the same figure disagree (typically OCR misreads of a
scanned report), the candidate that satisfies more accounting identities is
used and the conflict is written to the discrepancy log. Every value keeps
its document evidence id, page and raw label."""
from __future__ import annotations

import datetime as dt
import re

from . import pdf_tables as T

PDF_IDENTITIES = [
    ("gross_profit", ["revenue", "cogs"]),
    ("operating_income", ["gross_profit", "distribution", "admin", "other_opex"]),
    ("ebit_before_finance", ["operating_income", "other_income"]),
    ("pretax_before_levy", ["ebit_before_finance", "interest_expense"]),
    ("net_income", ["pretax_income", "income_tax"]),
    ("total_assets", ["total_liabilities_and_equity"]),
    ("net_change_cash", ["cfo", "cfi", "cff"]),
]


def _fy_end(year: int, fye_month: int) -> str:
    last = (dt.date(year + (fye_month == 12), fye_month % 12 + 1, 1) - dt.timedelta(days=1))
    return last.isoformat()


def _pages(pdf, must, any_of=None, exclude=None, start=0):
    return T.find_pages(pdf, must, any_of, exclude, start)


def _first_table(pdf, pages, rules, need: str, ncols: int | None = None):
    for p in pages:
        t = T.parse_page(pdf, p)
        if not t or (ncols and len(t.columns) != ncols):
            continue
        m = T.map_rows(t, rules)
        if need in m:
            return t, m
    return None, None


def extract_annual_report(path: str, ev_id: str, filed: str, doc_title: str, rules: dict, fye_month: int, log: list) -> dict:
    """{(item, fy_label): [candidate dicts]}"""
    pdf = T.open_pdf(path)
    cands: dict = {}
    excl = ["HORIZONTAL", "VERTICAL", "SIX YEAR", "CONDENSED", "ANALYSIS"]

    def add(mapping, table, source, priority, year_from_col=True):
        for item, cols in mapping.items():
            for col, (v, raw_label, raw_tok) in cols.items():
                if not re.fullmatch(r"(19|20)\d\d", str(col)):
                    continue
                fy = int(col)
                cands.setdefault((item, fy), []).append({
                    "value": v, "source": source, "priority": priority, "page": table.page, "raw_label": raw_label,
                    "raw_token": raw_tok, "evidence_id": ev_id, "filed": filed, "doc": doc_title})
    specs = [
        ("balance_sheet", ["STATEMENT OF FINANCIAL POSITION", "TOTAL ASSETS"], "total_assets"),
        ("income_statement", ["STATEMENT OF PROFIT OR LOSS", "PER SHARE"], "revenue"),
        ("cash_flow", ["STATEMENT OF CASH FLOWS", "OPERATING ACTIVITIES"], "cfo"),
    ]
    first_stmt_page = None
    for kind, must, need in specs:
        t, m = _first_table(pdf, _pages(pdf, must, ["NOTE"], excl), rules[kind], need, ncols=2)
        if t:
            add(m, t, "audited statements", 1)
            log.append(f"{doc_title}: audited {kind.replace('_', ' ')} parsed from p.{t.page} ({len(m)} items)")
            first_stmt_page = min(first_stmt_page or 10 ** 6, t.page)
        else:
            log.append(f"{doc_title}: audited {kind.replace('_', ' ')} NOT found")
    for kind, must, need in [("balance_sheet", ["SIX YEAR", "FINANCIAL POSITION"], "total_assets"),
                             ("income_statement", ["SIX YEAR", "PROFIT OR LOSS"], "revenue"),
                             ("cash_flow", ["SIX YEAR", "CASH FLOW"], "cfo")]:
        t, m = _first_table(pdf, _pages(pdf, must), rules[kind], need)
        if t:
            add(m, t, "six-year summary (company-prepared, unaudited presentation)", 2)
            log.append(f"{doc_title}: six-year {kind.replace('_', ' ')} parsed from p.{t.page} ({len(m)} items, {len(t.columns)} years)")
    # depreciation from the cash-generated-from-operations note
    for p in _pages(pdf, ["DEPRECIATION ON PROPERTY"], start=(first_stmt_page or 1) - 1):
        t = T.parse_page(pdf, p)
        if t and len(t.columns) == 2:
            m = T.map_rows(t, {"dna_note": rules["cash_flow"]["dna_note"], "amortisation_note": rules["cash_flow"]["amortisation_note"]})
            if "dna_note" in m:
                add(m, t, "notes to the audited statements", 1)
                log.append(f"{doc_title}: depreciation/amortisation from note on p.{t.page}")
                break
    return cands


def extract_quarterly(path: str, ev_id: str, filed: str, doc_title: str, rules: dict, log: list) -> dict:
    """Condensed interim statements: column 0 = current period, column 1 = comparative."""
    pdf = T.open_pdf(path)
    out = {"pl": {}, "bs": {}, "pl_prior": {}, "bs_prior": {}, "pages": {}, "period_text": None}
    for kind, must, need, key in [("income_statement", ["CONDENSED INTERIM STATEMENT OF PROFIT OR LOSS"], "revenue", "pl"),
                                  ("balance_sheet", ["CONDENSED INTERIM STATEMENT OF FINANCIAL POSITION"], "total_assets", "bs"),
                                  ("cash_flow", ["CONDENSED INTERIM STATEMENT OF CASH FLOWS"], "cfo", "cf")]:
        for p in _pages(pdf, must, ["NOTE", "RUPEES"]):
            t = T.parse_page(pdf, p)
            if not t or len(t.columns) != 2:
                continue
            m = T.map_rows(t, rules[kind])
            if need not in m:
                continue
            cur, prior = {}, {}
            c0, c1 = t.columns[0], t.columns[1]
            for item, cols in m.items():
                if c0 in cols:
                    cur[item] = {"value": cols[c0][0], "page": t.page, "raw_label": cols[c0][1], "evidence_id": ev_id, "doc": doc_title}
                if c1 in cols:
                    prior[item] = {"value": cols[c1][0], "page": t.page, "raw_label": cols[c1][1], "evidence_id": ev_id, "doc": doc_title}
            out[key] = cur
            out[key + "_prior"] = prior
            out["pages"][key] = t.page
            if key == "pl":
                mm = re.search(r"(QUARTER|HALF YEAR|NINE MONTHS|PERIOD) ENDED ([A-Z]+ \d{1,2}, \d{4})", t.heading.upper())
                out["period_text"] = mm.group(0) if mm else None
                if mm:
                    out["period_end"] = dt.datetime.strptime(mm.group(2).title(), "%B %d, %Y").date().isoformat()
                    out["months"] = {"QUARTER": 3, "HALF YEAR": 6, "NINE MONTHS": 9}.get(mm.group(1), 3)
            log.append(f"{doc_title}: condensed {kind.replace('_', ' ')} parsed from p.{t.page}")
            break
    return out


def _check(vals: dict, target: str, parts: list[str]) -> bool | None:
    if target not in vals or any(p not in vals for p in parts):
        return None
    s = sum(vals[p] for p in parts)
    return abs(vals[target] - s) <= 2.0   # statements are in whole rupees: allow rounding only


def resolve(cands: dict, years: list[int], discrepancies: list) -> dict:
    """Choose one value per (item, year); OCR conflicts resolved by identities."""
    chosen: dict = {}
    for (item, fy), lst in cands.items():
        chosen.setdefault(fy, {})[item] = sorted(lst, key=lambda c: c["priority"])[0]
    for fy in years:
        vals = {k: v["value"] for k, v in chosen.get(fy, {}).items()}
        for (item, y), lst in cands.items():
            if y != fy:
                continue
            distinct = {round(c["value"]) for c in lst}
            if len(distinct) <= 1:
                continue
            if max(distinct) - min(distinct) <= 2:   # rounding differences between presentations
                continue
            # definitional differences, not misreads: six-year summaries often show pre-tax BEFORE levy and tax INCLUDING levy
            prim = sorted(lst, key=lambda c: c["priority"])[0]
            alt_defs = {"pretax_income": vals.get("pretax_before_levy"),
                        "income_tax": (vals.get("income_tax", 0) + vals.get("levy", 0)) if "levy" in vals else None}
            if item in alt_defs and alt_defs[item] is not None and all(
                    abs(c["value"] - prim["value"]) <= 2 or abs(c["value"] - alt_defs[item]) <= 2 for c in lst):
                discrepancies.append({"type": "definition", "period": f"FY{fy}", "item": item, "diff": max(distinct) - min(distinct),
                                      "resolution": "Presentation difference, not an error: the six-year summary combines the levy with "
                                                    "taxation; the audited statement shows it separately. Canonical pre-tax is before levy; "
                                                    "tax includes levy (both sources then agree)."})
                continue
            best, best_score = None, -1
            for c in lst:
                trial = dict(vals, **{item: c["value"]})
                score = sum(1 for tgt, parts in PDF_IDENTITIES if item in [tgt] + parts and _check(trial, tgt, parts))
                if score > best_score or (score == best_score and best and c["priority"] < best["priority"]):
                    best, best_score = c, score
            alts = "; ".join(f"{c['source']} p.{c['page']}: {c['value']:,.0f} (token '{c['raw_token']}')" for c in lst)
            discrepancies.append({"type": "source_conflict", "period": f"FY{fy}", "item": item,
                                  "diff": max(distinct) - min(distinct),
                                  "resolution": f"Candidates: {alts}. Used {best['value']:,.0f} from {best['source']} p.{best['page']} "
                                                f"because it satisfies {best_score} accounting identit{'y' if best_score == 1 else 'ies'} "
                                                f"(likely OCR misread in the other source)."})
            chosen[fy][item] = best
            vals[item] = best["value"]
    return chosen


def to_canonical(chosen: dict, fye_month: int, currency: str) -> dict:
    years = sorted(chosen)
    periods, values, prov = [], {}, {}
    for fy in years:
        end = _fy_end(fy, fye_month)
        prev_end = dt.date.fromisoformat(_fy_end(fy - 1, fye_month))
        periods.append({"label": f"FY{fy}", "end": end, "start": (prev_end + dt.timedelta(days=1)).isoformat()})
        c = chosen[fy]
        v = {k: x["value"] for k, x in c.items()}

        def put(item, val, srcs):
            if val is None:
                return
            values.setdefault(item, {})[end] = val
            s = [c[k] for k in srcs if k in c]
            if s:
                prov.setdefault(item, {})[end] = {
                    "concept": "PDF: " + " + ".join(x["raw_label"] for x in s)[:120],
                    "accn": f"{s[0]['evidence_id']} p.{s[0]['page']}", "form": f"{s[0]['doc']} - {s[0]['source']}",
                    "filed": s[0]["filed"], "end": end, "start": None, "unit": currency}

        def g(k):
            return v.get(k)
        put("revenue", g("revenue"), ["revenue"])
        put("cogs", -g("cogs") if g("cogs") is not None else None, ["cogs"])
        put("gross_profit", g("gross_profit"), ["gross_profit"])
        if g("distribution") is not None or g("admin") is not None:
            put("sga", -((g("distribution") or 0) + (g("admin") or 0)), ["distribution", "admin"])
        if g("total_opex") is not None:
            put("operating_expenses", -g("total_opex"), ["total_opex"])
        put("operating_income", g("operating_income"), ["operating_income"])
        put("other_income", g("other_income"), ["other_income"])
        put("interest_expense", -g("interest_expense") if g("interest_expense") is not None else None, ["interest_expense"])
        pbl = g("pretax_before_levy")
        if pbl is None and g("pretax_income") is not None:
            pbl = g("pretax_income") - (g("levy") or 0)
        put("pretax_income", pbl, ["pretax_before_levy"] if g("pretax_before_levy") is not None else ["pretax_income"])
        if g("net_income") is not None and pbl is not None:
            put("income_tax", pbl - g("net_income"), ["income_tax", "levy"])
        put("net_income", g("net_income"), ["net_income"])
        put("eps_diluted", g("eps_diluted"), ["eps_diluted"])
        if g("eps_diluted") and g("net_income"):
            put("shares_diluted_weighted", round(g("net_income") / g("eps_diluted")), ["eps_diluted", "net_income"])
        elif g("share_capital"):
            put("shares_diluted_weighted", g("share_capital") / 10.0, ["share_capital"])
        if g("dna_note") is not None:
            put("dna", abs(g("dna_note")) + abs(g("amortisation_note") or 0), ["dna_note", "amortisation_note"])
        for k in ("cash", "short_term_investments", "receivables", "other_receivables", "total_current_assets", "ppe_net",
                  "intangibles", "total_assets", "accounts_payable", "short_term_debt", "current_portion_ltd", "long_term_debt",
                  "total_current_liabilities", "total_equity", "total_liabilities_and_equity", "cfo", "capex", "cfi",
                  "dividends_paid", "cff", "cash_end_cf"):
            put(k, g(k), [k])
        if g("inventory") is not None or g("stores") is not None:
            put("inventory", (g("inventory") or 0) + (g("stores") or 0), ["inventory", "stores"])
        if g("net_change_cash") is not None:
            put("net_change_cash", g("net_change_cash"), ["net_change_cash"])
    return {"taxonomy": "IFRS (Pakistan) - PDF extraction", "currency": currency, "periods": periods, "values": values,
            "prov": prov, "restatements": []}


def interim_canonical(q: dict, annual: dict, currency: str) -> dict | None:
    if not q.get("pl") or not q.get("period_end"):
        return None

    def conv(d):
        g = {k: x["value"] for k, x in d.items()}
        out = {}
        if "revenue" in g:
            out["revenue"] = g["revenue"]
        if "cogs" in g:
            out["cogs"] = -g["cogs"]
        if "gross_profit" in g:
            out["gross_profit"] = g["gross_profit"]
        if "operating_income" in g:
            out["operating_income"] = g["operating_income"]
        if "interest_expense" in g:
            out["interest_expense"] = -g["interest_expense"]
        if "other_income" in g:
            out["other_income"] = g["other_income"]
        pbl = g.get("pretax_before_levy", g.get("pretax_income"))
        if pbl is not None:
            out["pretax_income"] = pbl
        if "net_income" in g and pbl is not None:
            out["net_income"] = g["net_income"]
            out["income_tax"] = pbl - g["net_income"]
        if "eps_diluted" in g:
            out["eps_diluted"] = g["eps_diluted"]
        for k in ("cfo", "capex", "dividends_paid"):
            if k in g:
                out[k] = g[k]
        return out
    ytd = conv(q["pl"]) | conv(q.get("cf", {}))
    ytd_prior = conv(q["pl_prior"]) | conv(q.get("cf_prior", {}))
    bs_raw = {k: x["value"] for k, x in q["bs"].items()}
    bs = {k: bs_raw[k] for k in ("cash", "short_term_investments", "receivables", "other_receivables", "total_assets",
                                 "accounts_payable", "short_term_debt", "current_portion_ltd", "long_term_debt", "total_equity",
                                 "ppe_net", "total_liabilities_and_equity", "total_current_assets", "total_current_liabilities") if k in bs_raw}
    if "inventory" in bs_raw or "stores" in bs_raw:
        bs["inventory"] = bs_raw.get("inventory", 0) + bs_raw.get("stores", 0)
    last = annual["periods"][-1]["end"]
    ttm = {}
    for k in ("revenue", "cogs", "gross_profit", "operating_income", "interest_expense", "pretax_income", "income_tax", "net_income",
              "other_income", "cfo", "capex", "dividends_paid"):
        fy = annual["values"].get(k, {}).get(last)
        if fy is not None and k in ytd and k in ytd_prior:
            ttm[k] = fy + ytd[k] - ytd_prior[k]
    prov = {f"bs.{k}": {"concept": f"PDF: {x['raw_label']}", "accn": f"{x['evidence_id']} p.{x['page']}", "form": x["doc"],
                        "filed": None, "end": q["period_end"], "unit": currency} for k, x in q["bs"].items()}
    return {"end": q["period_end"], "months": q.get("months", 3), "ytd": ytd, "ytd_prior": ytd_prior, "bs": bs, "ttm": ttm,
            "prov": prov, "period_text": q.get("period_text"), "pages": q["pages"]}


def cross_check_vendor(annual: dict, vendor: dict, discrepancies: list, ev_id: str) -> list[dict]:
    """Compare reconciled revenue / PAT / EPS with the PSX portal's vendor summary (tier 4)."""
    checks = []
    for p in annual["periods"]:
        fy = p["label"][2:]
        vrow = vendor.get(fy) or {}
        for vk, ck, scale in (("Sales", "revenue", 1000), ("Profit after Taxation", "net_income", 1000), ("EPS", "eps_diluted", 1)):
            vv, cv = vrow.get(vk), annual["values"].get(ck, {}).get(p["end"])
            if vv is None or cv is None:
                continue
            ok = abs(vv * scale - cv) <= max(abs(cv) * 0.001, scale * 1.5)
            checks.append({"check": f"{vk} vs PSX portal summary", "period": p["label"], "diff": vv * scale - cv, "pass": ok,
                           "detail": f"PSX portal (Capital Stake) [{ev_id}]"})
            if not ok:
                discrepancies.append({"type": "vendor_cross_check", "period": p["label"], "item": ck, "diff": vv * scale - cv,
                                      "resolution": "Issuer document retained (tier 1); vendor figure may be standardised."})
    return checks
