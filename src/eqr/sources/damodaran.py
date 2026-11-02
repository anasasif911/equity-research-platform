"""Damodaran Online datasets (tier 3): country risk premiums and
emerging-market industry betas. Values are parsed from the retrieved files
and carry their publication dates."""
from __future__ import annotations

import datetime as dt
import html
import io
import re

from .evidence import EvidenceStore

CTRYPREM = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ctryprem.html"
BETA_EM = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betaemerg.xls"


def _pct(s: str) -> float | None:
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", s)
    return float(m.group(1)) / 100 if m else None


def parse_ctryprem(page: str, country: str) -> dict:
    upd = re.search(r"Last updated:\s*([A-Za-z]+ \d{1,2}, \d{4})", page)
    as_of = dt.datetime.strptime(upd.group(1), "%B %d, %Y").date().isoformat() if upd else None
    i = page.find(f">{country}<")
    if i < 0:
        i = page.find(country)
    if i < 0:
        raise ValueError(f"{country} not found in country risk table")
    cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", page[i - 200: i + 2000], re.S)]
    k = next(j for j, c in enumerate(cells) if c == country)
    row = cells[k: k + 8]
    return {"country": country, "rating": row[1], "default_spread": _pct(row[2]), "crp": _pct(row[3]),
            "total_erp": _pct(row[4]), "corporate_tax": _pct(row[5]), "as_of": as_of,
            "columns": "Country | Moody's rating | Adj. default spread | Country risk premium | Equity risk premium | Corporate tax rate"}


def country_risk(store: EvidenceStore, country: str) -> dict:
    data, ev = store.fetch(CTRYPREM, kind="damodaran_ctryprem", title="Damodaran Online - country default spreads and risk premiums",
                           publisher="A. Damodaran, NYU Stern", tier=3, ttl_hours=168, critical=True,
                           purpose="Sovereign default spread and country risk premium")
    out = parse_ctryprem(data.decode("utf-8", errors="ignore"), country)
    ev.period = out["as_of"]
    store.save()
    out["evidence_id"] = ev.id
    return out


def parse_beta_xls(data: bytes, industry: str) -> dict:
    import xlrd
    b = xlrd.open_workbook(file_contents=data)
    sh = b.sheet_by_name("Industry Averages")
    as_of = None
    for r in range(min(5, sh.nrows)):
        if str(sh.cell_value(r, 0)).lower().startswith("date updated"):
            as_of = (dt.date(1899, 12, 30) + dt.timedelta(days=int(sh.cell_value(r, 1)))).isoformat()
    hdr_row = next(r for r in range(sh.nrows) if str(sh.cell_value(r, 0)).strip() == "Industry Name")
    hdr = [str(sh.cell_value(hdr_row, c)).strip() for c in range(sh.ncols)]
    for r in range(hdr_row + 1, sh.nrows):
        if str(sh.cell_value(r, 0)).strip().lower() == industry.lower():
            row = {h: sh.cell_value(r, c) for c, h in enumerate(hdr)}
            return {"industry": industry, "n_firms": int(row["Number of firms"]), "levered_beta": float(row["Beta"]),
                    "de_ratio": float(row["D/E Ratio"]), "unlevered_beta": float(row["Unlevered beta"]),
                    "unlevered_beta_cash_corrected": float(row["Unlevered beta corrected for cash"]), "as_of": as_of,
                    "universe": "Emerging markets"}
    raise ValueError(f"industry '{industry}' not found")


def industry_beta(store: EvidenceStore, industry: str) -> dict:
    data, ev = store.fetch(BETA_EM, kind="damodaran_beta_em", title="Damodaran Online - emerging-market industry betas",
                           publisher="A. Damodaran, NYU Stern", tier=3, ttl_hours=168, critical=True,
                           purpose="Bottom-up (industry) unlevered beta")
    out = parse_beta_xls(data, industry)
    ev.period = out["as_of"]
    store.save()
    out["evidence_id"] = ev.id
    return out


def relever(beta_u: float, de: float, tax: float) -> float:
    return beta_u * (1 + (1 - tax) * de)
