"""Risk-free rate (US Treasury, tier 2) and equity risk premium (Damodaran
implied ERP, tier 3). Values are parsed from retrieved documents only."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re

from .evidence import EvidenceStore, SourceUnavailable

TREASURY_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                "{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
DAMODARAN_HOME = "https://pages.stern.nyu.edu/~adamodar/"


def parse_treasury_csv(data: bytes, cutoff: dt.date, tenor: str = "10 Yr") -> dict:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", errors="ignore"))))
    best = None
    for r in rows:
        d = dt.datetime.strptime(r["Date"], "%m/%d/%Y").date()
        if d <= cutoff and r.get(tenor) not in (None, ""):
            if best is None or d > best[0]:
                best = (d, float(r[tenor]) / 100, r)
    if not best:
        raise ValueError("no Treasury observation on/before cutoff")
    curve = {k: float(v) / 100 for k, v in best[2].items() if k != "Date" and v not in ("", None)}
    return {"value": best[1], "as_of": best[0].isoformat(), "tenor": tenor, "curve": curve}


def risk_free_usd(store: EvidenceStore, cutoff: dt.date) -> dict | None:
    for year in (cutoff.year, cutoff.year - 1):
        url = TREASURY_CSV.format(year=year)
        try:
            data, ev = store.fetch(url, kind="treasury_par_curve", title=f"US Treasury daily par yield curve {year}",
                                   publisher="U.S. Department of the Treasury", tier=2, ttl_hours=12,
                                   critical=True, purpose="Risk-free rate (10-year)")
            out = parse_treasury_csv(data, cutoff)
            out["evidence_id"] = ev.id
            ev.period = out["as_of"]
            store.save()
            return out
        except (SourceUnavailable, ValueError, KeyError):
            continue
    return None


ERP_PATTERNS = [
    r"implied\s+(?:equity\s+)?(?:risk\s+)?premium[^%]{0,160}?(\d{1,2}\.\d{1,2})\s*%",
    r"implied\s+erp[^%]{0,160}?(\d{1,2}\.\d{1,2})\s*%",
]


def parse_damodaran_erp(text: str) -> dict:
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\s+", " ", t)
    for pat in ERP_PATTERNS:
        m = re.search(pat, t, re.I)
        if m:
            ctx = t[max(0, m.start() - 120): m.end() + 40]
            date_m = re.search(r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})", ctx)
            as_of = None
            if date_m:
                try:
                    as_of = dt.datetime.strptime(date_m.group(1).replace(",", ""), "%B %d %Y").date().isoformat()
                except ValueError:
                    as_of = None
            return {"value": float(m.group(1)) / 100, "as_of": as_of, "context": ctx.strip()}
    raise ValueError("implied ERP not found in document")


def equity_risk_premium(store: EvidenceStore) -> dict | None:
    try:
        data, ev = store.fetch(DAMODARAN_HOME, kind="damodaran_erp", title="Damodaran Online - current implied ERP",
                               publisher="A. Damodaran, NYU Stern", tier=3, ttl_hours=168, critical=True,
                               purpose="Mature-market equity risk premium (implied, S&P 500)")
    except SourceUnavailable:
        return None
    text = data.decode("utf-8", errors="ignore")
    try:
        if text.lstrip().startswith("{"):          # structured inbox override
            js = json.loads(text)
            out = {"value": float(js["value"]), "as_of": js.get("as_of"), "context": js.get("context", "")}
        else:
            out = parse_damodaran_erp(text)
    except (ValueError, KeyError):
        return None
    out["evidence_id"] = ev.id
    ev.period = out.get("as_of")
    store.save()
    return out
