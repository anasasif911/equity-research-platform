"""SEC companyfacts (XBRL) -> canonical financial series with per-value
provenance. Pure functions: no network, fully unit-tested.

Notes on XBRL semantics handled here:
* `fy`/`fp` on a fact describe the FILING, not the period; periods are keyed
  by their own start/end dates.
* The same period appears in several filings (comparatives, restatements):
  the latest-filed value wins and earlier differing values are logged as
  restatements in the discrepancy log.
* 52/53-week fiscal years: annual durations accepted within 340-380 days.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any

from ..util import parse_date

CURRENCY_UNITS = {"USD", "EUR", "GBP", "JPY", "CNY", "CAD", "AUD", "CHF", "INR", "PKR", "HKD", "KRW", "TWD",
                  "SEK", "NOK", "DKK", "BRL", "MXN", "ZAR", "SGD", "ILS"}

SECTIONS = ("income_statement", "balance_sheet", "cash_flow")


def _days(f: dict) -> int | None:
    if not f.get("start"):
        return None
    return (parse_date(f["end"]) - parse_date(f["start"])).days


def detect_taxonomy_and_currency(facts: dict) -> tuple[str, str]:
    tax = "ifrs-full" if "ifrs-full" in facts.get("facts", {}) and len(facts["facts"]["ifrs-full"]) > \
        len(facts["facts"].get("us-gaap", {})) else "us-gaap"
    cnt: Counter = Counter()
    for concept in facts["facts"].get(tax, {}).values():
        for unit, arr in concept.get("units", {}).items():
            if unit in CURRENCY_UNITS:
                cnt[unit] += len(arr)
    cur = cnt.most_common(1)[0][0] if cnt else "USD"
    return tax, cur


def _unit_for(rule: dict, currency: str) -> str:
    u = rule.get("unit")
    if u == "USD/shares":
        return f"{currency}/shares"
    return u or currency


def _concept_facts(facts: dict, taxonomy: str, concept: str, unit: str) -> list[dict]:
    c = facts.get("facts", {}).get(taxonomy, {}).get(concept)
    if not c:
        return []
    return c.get("units", {}).get(unit, [])


def _candidates(rule: dict, taxonomy: str) -> list[tuple[str, str]]:
    out = []
    for tx in (taxonomy, "dei"):
        for concept in rule.get(tx, []) or []:
            out.append((tx, concept))
    return out


def _pick_latest(fs: list[dict]) -> tuple[dict, list[dict]]:
    fs = sorted(fs, key=lambda f: (f.get("filed", ""), f.get("accn", "")))
    chosen = fs[-1]
    restated = [f for f in fs[:-1] if abs(f["val"] - chosen["val"]) > max(1.0, abs(chosen["val"]) * 1e-6)]
    return chosen, restated


def annual_period_ends(facts: dict, taxonomy: str, currency: str, rules: dict, annual_forms: list[str]) -> list[dict]:
    """Fiscal-year periods (start,end) from revenue / net income annual facts."""
    lo, hi = rules["periods"]["annual_duration_days"]
    periods: dict[str, str] = {}
    for item in ("revenue", "net_income", "operating_income", "cfo"):
        sec = next(s for s in SECTIONS if item in rules[s])
        for tx, concept in _candidates(rules[sec][item], taxonomy):
            for f in _concept_facts(facts, tx, concept, currency):
                d = _days(f)
                if f.get("form") in annual_forms and d and lo <= d <= hi:
                    periods.setdefault(f["end"], f["start"])
    # merge ends within 7 days (rare tagging inconsistencies)
    ends = sorted(periods)
    merged: list[dict] = []
    for e in ends:
        if merged and (parse_date(e) - parse_date(merged[-1]["end"])).days < 300:
            continue
        merged.append({"end": e, "start": periods[e]})
    for p in merged:
        p["label"] = f"FY{parse_date(p['end']).year if parse_date(p['end']).month > 3 else parse_date(p['end']).year - 1}"
    return merged


def _match_end(f_end: str, target: str, tol: int = 7) -> bool:
    return abs((parse_date(f_end) - parse_date(target)).days) <= tol


def extract_value(facts: dict, taxonomy: str, currency: str, rule: dict, *, end: str, start: str | None,
                  forms: list[str] | None, duration_range: tuple[int, int] | None) -> tuple[float | None, dict | None, list[dict]]:
    """Value of a canonical item for one period. Tries candidates in order,
    then the `sum_<taxonomy>` component rule."""
    unit = _unit_for(rule, currency)
    for tx, concept in _candidates(rule, taxonomy):
        fs = []
        for f in _concept_facts(facts, tx, concept, unit):
            if forms and f.get("form") not in forms:
                continue
            if not _match_end(f["end"], end, 3):
                continue
            if duration_range is None:  # instant
                if f.get("start"):
                    continue
            else:
                d = _days(f)
                if d is None or not (duration_range[0] <= d <= duration_range[1]):
                    continue
                if start and not _match_end(f["start"], start, 7):
                    continue
            fs.append(f)
        if fs:
            chosen, restated = _pick_latest(fs)
            return float(chosen["val"]), {"concept": f"{tx}:{concept}", "accn": chosen.get("accn"),
                                          "form": chosen.get("form"), "filed": chosen.get("filed"),
                                          "end": chosen["end"], "start": chosen.get("start"), "unit": unit}, restated
    comps = rule.get(f"sum_{taxonomy}")
    if comps:
        total, provs = 0.0, []
        for c in comps:
            v, p, _ = extract_value(facts, taxonomy, currency, {taxonomy: [c], "unit": rule.get("unit")}, end=end,
                                    start=start, forms=forms, duration_range=duration_range)
            if v is None:
                return None, None, []
            total += v
            provs.append(p)
        return total, {"concept": "sum(" + ",".join(p["concept"] for p in provs) + ")", "accn": provs[0]["accn"],
                       "form": provs[0]["form"], "filed": provs[0]["filed"], "end": end, "start": start,
                       "unit": provs[0]["unit"]}, []
    return None, None, []


def _is_instant(section: str, rule: dict) -> bool:
    return section == "balance_sheet" or rule.get("instant", False)


def build_annual(facts: dict, rules: dict, max_years: int = 10) -> dict:
    taxonomy, currency = detect_taxonomy_and_currency(facts)
    annual_forms = rules["periods"]["annual_forms"]
    lo, hi = rules["periods"]["annual_duration_days"]
    periods = annual_period_ends(facts, taxonomy, currency, rules, annual_forms)[-max_years:]
    values: dict[str, dict[str, float]] = defaultdict(dict)
    prov: dict[str, dict[str, dict]] = defaultdict(dict)
    restatements: list[dict] = []
    for sec in SECTIONS:
        for item, rule in rules[sec].items():
            if item == "shares_outstanding":
                continue
            instant = _is_instant(sec, rule)
            for p in periods:
                v, pv, rest = extract_value(facts, taxonomy, currency, rule, end=p["end"],
                                            start=None if instant else p["start"], forms=annual_forms,
                                            duration_range=None if instant else (lo, hi))
                if v is None:
                    continue
                if rule.get("sign") == "outflow":
                    v = -abs(v)
                values[item][p["end"]] = v
                prov[item][p["end"]] = pv
                for r in rest:
                    restatements.append({"item": item, "period_end": p["end"], "original": r["val"],
                                         "original_accn": r.get("accn"), "original_filed": r.get("filed"),
                                         "restated": v, "restated_accn": pv["accn"]})
    return {"taxonomy": taxonomy, "currency": currency, "periods": periods, "values": dict(values),
            "prov": dict(prov), "restatements": restatements, "entity": facts.get("entityName"),
            "cik": facts.get("cik")}


def build_interim(facts: dict, rules: dict, annual: dict) -> dict | None:
    """Latest interim period after the last fiscal year: YTD, prior-year YTD,
    balance sheet, and trailing-twelve-month (TTM = FY + YTD - prior YTD)."""
    if not annual["periods"]:
        return None
    taxonomy, currency = annual["taxonomy"], annual["currency"]
    last = annual["periods"][-1]
    fy_end = parse_date(last["end"])
    interim_forms = rules["periods"]["interim_forms"]
    # latest quarter end from revenue/net income interim facts
    q_ends = set()
    for item in ("revenue", "net_income"):
        for tx, concept in _candidates(rules["income_statement"][item], taxonomy):
            for f in _concept_facts(facts, tx, concept, currency):
                if f.get("form") in interim_forms and parse_date(f["end"]) > fy_end + dt.timedelta(days=20):
                    q_ends.add(f["end"])
    if not q_ends:
        return None
    q_end = max(q_ends)
    ytd_start = (fy_end + dt.timedelta(days=1)).isoformat()
    ytd_days = (parse_date(q_end) - fy_end).days
    prior_end = (parse_date(q_end) - dt.timedelta(days=364)).isoformat()
    prior_start = annual["periods"][-1]["start"]
    out: dict[str, Any] = {"end": q_end, "ytd_start": ytd_start, "ytd_days": ytd_days, "prior_end": prior_end,
                           "ytd": {}, "ytd_prior": {}, "bs": {}, "ttm": {}, "prov": {}}
    rng = (ytd_days - 15, ytd_days + 15)
    for sec in SECTIONS:
        for item, rule in rules[sec].items():
            if item == "shares_outstanding":
                continue
            if _is_instant(sec, rule):
                v, pv, _ = extract_value(facts, taxonomy, currency, rule, end=q_end, start=None, forms=None,
                                         duration_range=None)
                if v is not None:
                    out["bs"][item] = v
                    out["prov"][f"bs.{item}"] = pv
                continue
            v, pv, _ = extract_value(facts, taxonomy, currency, rule, end=q_end, start=ytd_start, forms=None, duration_range=rng)
            vp, pvp, _ = extract_value(facts, taxonomy, currency, rule, end=prior_end, start=prior_start, forms=None,
                                       duration_range=rng)
            sign = -1 if rule.get("sign") == "outflow" else 1
            if v is not None:
                out["ytd"][item] = sign * abs(v) if sign < 0 else v
                out["prov"][f"ytd.{item}"] = pv
            if vp is not None:
                out["ytd_prior"][item] = sign * abs(vp) if sign < 0 else vp
                out["prov"][f"ytd_prior.{item}"] = pvp
            fy_v = annual["values"].get(item, {}).get(last["end"])
            if v is not None and vp is not None and fy_v is not None:
                out["ttm"][item] = fy_v + out["ytd"][item] - out["ytd_prior"][item]
    return out


def shares_outstanding(facts: dict, rules: dict) -> dict | None:
    rule = rules["balance_sheet"]["shares_outstanding"]
    best = None
    for tx in ("dei", "us-gaap", "ifrs-full"):
        for concept in rule.get(tx, []) or []:
            for f in _concept_facts(facts, tx, concept, "shares"):
                if best is None or (f["end"], f.get("filed", "")) > (best[1]["end"], best[1].get("filed", "")):
                    best = (f"{tx}:{concept}", f)
    if not best:
        return None
    c, f = best
    return {"value": float(f["val"]), "as_of": f["end"], "concept": c, "accn": f.get("accn"), "form": f.get("form"),
            "filed": f.get("filed")}


def dividends_per_share(facts: dict, taxonomy: str, currency: str, periods: list[dict], annual_forms: list[str]) -> dict:
    out = {}
    for concept in ("CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"):
        for p in periods:
            if p["end"] in out:
                continue
            for f in _concept_facts(facts, taxonomy, concept, f"{currency}/shares"):
                d = _days(f)
                if f.get("form") in annual_forms and d and 340 <= d <= 380 and _match_end(f["end"], p["end"], 3):
                    out[p["end"]] = float(f["val"])
    return out


def segment_like_facts(facts: dict, taxonomy: str) -> list[str]:
    """Names of company-specific (extension) concepts, used as a hint for KPI
    coverage. Segment members are not in companyfacts (dimensional facts are
    excluded by the SEC API) - segment data therefore comes from filing text."""
    return sorted(k for k in facts.get("facts", {}) if k not in ("us-gaap", "ifrs-full", "dei", "srt", "invest"))
