"""Valuation orchestration: method selection, scenario valuation, reverse DCF,
trading comparables, DDM, residual income, and reconciliation.

All DCF numbers come from evaluating the same ModelSpec that is written to
Excel (see model/sheetmodel.py)."""
from __future__ import annotations

import copy
import statistics
from typing import Callable

from ..model import build as B
from ..model.sheetmodel import evaluate
from ..util import fmt_pct, safe_div

SCEN = ["base", "bull", "bear", "stress"]


# ----------------------------------------------------------------------------- selection
def select_methods(rules: dict, archetype: str, avail: dict, playbook: dict) -> list[dict]:
    """avail: flags such as historical_financials, market_price, risk_free_rate,
    equity_risk_premium, beta, dividend_history, payout_ok, peer_set_with_market_data,
    segment_profit_disclosure, book_value."""
    out = []
    implemented = {"fcff_dcf", "reverse_dcf", "trading_comps", "ddm", "residual_income"}
    for mid, m in rules["methods"].items():
        reasons = []
        status = "selected"
        if archetype in (m.get("unsuitable_archetypes") or []):
            status, reasons = "rejected", [f"unsuitable for {archetype} archetype ({m.get('rationale', '')})"]
        missing = [r for r in m.get("requires", []) if r not in ("fcff_dcf",) and not avail.get(r)]
        if status == "selected" and missing:
            status, reasons = "rejected", [f"required inputs unavailable: {', '.join(missing)}"]
        if status == "selected" and mid == "ddm" and not avail.get("payout_ok"):
            status, reasons = "rejected", ["payout ratio outside 30-100% or dividends not paid consistently: distributions dominated by buybacks, so DDM would understate value"]
        if status == "selected" and mid == "fcfe_dcf":
            status, reasons = "rejected", ["redundant with FCFF for a firm with stable, low leverage; FCFF preferred (capital structure separable)"]
        if status == "selected" and mid in ("sotp",) and not avail.get("segment_profit_disclosure"):
            status, reasons = "rejected", ["segment operating profit not available in structured data"]
        if status == "selected" and mid == "residual_income" and archetype not in (m.get("preferred_archetypes") or []):
            status, reasons = "rejected", ["book value is not the binding value driver for this archetype; used only for financials"]
        pref = m.get("preferred_archetypes")
        if status == "selected" and pref and archetype not in pref and mid not in ("residual_income",):
            status, reasons = "rejected", [f"specialist framework for {', '.join(pref)}; not applicable"]
        if status == "selected" and mid == "precedent_transactions":
            status, reasons = "rejected", ["no sufficiently comparable public transaction set retrieved; not defensible"]
        if status == "selected" and mid == "reverse_dcf" and not avail.get("market_price"):
            status, reasons = "rejected", ["no sourced market price"]
        if status == "selected" and mid not in implemented:
            status, reasons = "rejected", ["framework not implemented in this platform version (see docs/LIMITATIONS.md)"]
        if status == "selected" and archetype in ("reit", "resource", "holding_company", "pre_revenue", "distressed") \
                and mid == "fcff_dcf":
            reasons.append("cross-check only: specialist framework for this archetype is not implemented")
        if status == "selected":
            reasons = reasons or [m.get("rationale", "")]
        out.append({"id": mid, "label": m["label"], "status": status, "reason": "; ".join(reasons)})
    w = rules["weights"].get(archetype) or rules["weights"]["default"]
    sel = [o for o in out if o["status"] == "selected" and o["id"] != "reverse_dcf"]
    tot = sum(w.get(o["id"], 0) for o in sel)
    for o in out:
        o["weight"] = (w.get(o["id"], 0) / tot) if (o in sel and tot) else 0.0
    return out


# ----------------------------------------------------------------------------- DCF scenarios
class ModelContext:
    """Everything needed to (re)build the spec; used for scenarios and solving."""

    def __init__(self, **kw):
        self.kw = kw

    def spec(self, scenario_index: int = 1, scenarios: dict | None = None):
        kw = dict(self.kw)
        if scenarios is not None:
            kw["scenarios"] = scenarios
        return B.build_spec(scenario_index=scenario_index, **kw)

    def evaluate(self, scenario_index: int = 1, scenarios: dict | None = None) -> dict:
        return evaluate(self.spec(scenario_index, scenarios))


def scenario_results(ctx: ModelContext) -> dict:
    out = {}
    for i, sc in enumerate(SCEN, start=1):
        r = ctx.evaluate(i)
        S = r["scalars"]
        rows = r["rows"]
        out[sc] = {"label": ctx.kw["scenarios"][sc]["label"], "probability": ctx.kw["scenarios"][sc]["probability"],
                   "value_per_share": S["value_per_share"], "ev": S["ev"], "equity_value": S["equity_value"],
                   "wacc": S["wacc"], "terminal_g": S["terminal_g"], "tv_share": S["tv_share"],
                   "target_12m": S["target_12m"], "upside": S["upside"],
                   "revenue_final": rows[("IS", "revenue")][-1], "ebit_margin_final": rows[("IS", "ebit_margin")][-1],
                   "eps_path": rows[("IS", "eps")][-ctx.kw["horizon"]:], "errors": r["errors"],
                   "definition": ctx.kw["scenarios"][sc]["definition"]}
    probs = {k: v["probability"] for k, v in out.items() if v["probability"]}
    out["probability_weighted_value"] = sum(out[k]["value_per_share"] * p for k, p in probs.items()) / sum(probs.values())
    return out


def _shifted(scenarios: dict, growth_shift=0.0, margin_shift=0.0) -> dict:
    sc = copy.deepcopy(scenarios)
    sc["base"]["drivers"]["growth"] = [g + growth_shift for g in sc["base"]["drivers"]["growth"]]
    sc["base"]["drivers"]["ebit_margin"] = [m + margin_shift for m in sc["base"]["drivers"]["ebit_margin"]]
    return sc


def _bisect(f: Callable[[float], float], lo: float, hi: float, tol=1e-5, it=80) -> float | None:
    flo, fhi = f(lo), f(hi)
    if flo is None or fhi is None or flo * fhi > 0:
        return None
    for _ in range(it):
        mid = (lo + hi) / 2
        fm = f(mid)
        if fm is None:
            return None
        if abs(fm) < tol or hi - lo < tol:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def reverse_dcf(ctx: ModelContext, price: float) -> dict:
    sc0 = ctx.kw["scenarios"]

    def gap_g(d):
        v = ctx.evaluate(1, _shifted(sc0, growth_shift=d))["scalars"]["value_per_share"]
        return None if v is None else v - price

    def gap_m(d):
        v = ctx.evaluate(1, _shifted(sc0, margin_shift=d))["scalars"]["value_per_share"]
        return None if v is None else v - price

    dg = _bisect(gap_g, -0.25, 0.40)
    dm = _bisect(gap_m, -0.40, 0.50)
    base_g = sc0["base"]["drivers"]["growth"]
    base_m = sc0["base"]["drivers"]["ebit_margin"]
    n = len(base_g)

    def cagr(path):
        p = 1.0
        for g in path:
            p *= 1 + g
        return p ** (1 / len(path)) - 1
    out = {"price": price, "growth_shift": dg, "margin_shift": dm,
           "model_revenue_cagr": cagr(base_g), "model_avg_margin": sum(base_m) / n}
    out["implied_revenue_cagr"] = cagr([g + dg for g in base_g]) if dg is not None else None
    out["implied_avg_margin"] = (sum(base_m) / n + dm) if dm is not None else None
    if dg is not None:
        out["narrative_growth"] = (f"Holding margins and reinvestment at base, the price of {price:,.2f} implies revenue "
                                   f"growth of {fmt_pct(out['implied_revenue_cagr'])} a year over {n} years versus "
                                   f"{fmt_pct(out['model_revenue_cagr'])} in our base case.")
    else:
        out["narrative_growth"] = "No growth path within -25pp/+40pp of base reconciles the price (solver bounds)."
    if dm is not None:
        out["narrative_margin"] = (f"Holding base growth, the price implies an average EBIT margin of "
                                   f"{fmt_pct(out['implied_avg_margin'])} versus {fmt_pct(out['model_avg_margin'])} in our base case.")
    else:
        out["narrative_margin"] = "No margin path within bounds reconciles the price."
    return out


def margin_growth_grid(ctx: ModelContext, steps=(-0.02, -0.01, 0.0, 0.01, 0.02)) -> dict:
    """Engine-computed (full re-forecast) value per share for EBIT margin x growth shifts."""
    sc0 = ctx.kw["scenarios"]
    grid = []
    for dm in steps:
        row = []
        for dg in steps:
            row.append(ctx.evaluate(1, _shifted(sc0, growth_shift=dg, margin_shift=dm))["scalars"]["value_per_share"])
        grid.append(row)
    return {"margin_shifts": list(steps), "growth_shifts": list(steps), "values": grid}


# ----------------------------------------------------------------------------- comps
def comps(target: dict, peers: list[dict]) -> dict:
    """target/peers: {ticker, price, shares, fin_assets, debt, nci, ttm_revenue, ttm_ebitda, ttm_ebit, ttm_net_income,
    evidence_ids}. Returns multiples, medians and implied values. Requires >= 3 valid peers."""
    rows = []
    for p in peers:
        mcap = p["price"] * p["shares"]
        ev = mcap + p["debt"] + p.get("nci", 0) - p["fin_assets"]
        rows.append({**p, "market_cap": mcap, "ev": ev,
                     "ev_sales": safe_div(ev, p.get("ttm_revenue")),
                     "ev_ebitda": safe_div(ev, p.get("ttm_ebitda")) if (p.get("ttm_ebitda") or 0) > 0 else None,
                     "pe": safe_div(mcap, p.get("ttm_net_income")) if (p.get("ttm_net_income") or 0) > 0 else None})

    def med(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return statistics.median(v) if len(v) >= 3 else None

    def q(k):
        v = sorted(r[k] for r in rows if r.get(k) is not None)
        if len(v) < 3:
            return None
        qs = statistics.quantiles(v, n=4)
        return qs[0], qs[2]
    medians = {k: med(k) for k in ("ev_sales", "ev_ebitda", "pe")}
    bridge = target["fin_assets"] - target["debt"] - target.get("nci", 0)
    implied = {}
    if medians["ev_ebitda"] and target.get("ttm_ebitda"):
        implied["ev_ebitda"] = (medians["ev_ebitda"] * target["ttm_ebitda"] + bridge) / target["shares"]
    if medians["pe"] and target.get("ttm_net_income"):
        implied["pe"] = medians["pe"] * target["ttm_net_income"] / target["shares"]
    tm = target["price"] * target["shares"]
    tgt_mult = {"ev_ebitda": safe_div(tm - bridge, target.get("ttm_ebitda")), "pe": safe_div(tm, target.get("ttm_net_income")),
                "ev_sales": safe_div(tm - bridge, target.get("ttm_revenue"))}
    central = statistics.mean(implied.values()) if implied else None
    return {"peers": rows, "medians": medians, "iqr": {k: q(k) for k in ("ev_ebitda", "pe")}, "implied": implied,
            "central": central, "target_multiples": tgt_mult, "n_valid": sum(1 for r in rows if r.get("ev_ebitda"))}


# ----------------------------------------------------------------------------- DDM / RI
def ddm(dps_path: list[float], ke: float, g: float, years_offset: float = 0.5) -> dict | None:
    if not dps_path or ke <= g:
        return None
    pv = sum(d / (1 + ke) ** (i + years_offset) for i, d in enumerate(dps_path))
    tv = dps_path[-1] * (1 + g) / (ke - g)
    pv_tv = tv / (1 + ke) ** (len(dps_path) - 1 + years_offset)
    return {"value": pv + pv_tv, "pv_dividends": pv, "pv_terminal": pv_tv}


def residual_income(bv0: float, roe_start: float, ke: float, g: float, payout: float, years: int, shares: float,
                    roe_fade_to: float | None = None) -> dict:
    roe_end = roe_fade_to if roe_fade_to is not None else ke + 0.5 * (roe_start - ke)
    bv, pv, rows = bv0, 0.0, []
    for t in range(1, years + 1):
        roe = roe_start + (roe_end - roe_start) * (t - 1) / max(1, years - 1)
        ni = roe * bv
        ri = (roe - ke) * bv
        pv += ri / (1 + ke) ** t
        rows.append({"t": t, "bv_open": bv, "roe": roe, "ni": ni, "ri": ri})
        bv = bv + ni * (1 - payout)
    tv = rows[-1]["ri"] * (1 + g) / (ke - g) if ke > g else None
    pv_tv = tv / (1 + ke) ** years if tv is not None else 0.0
    equity = bv0 + pv + pv_tv
    return {"rows": rows, "pv_ri": pv, "pv_tv": pv_tv, "equity_value": equity, "value_per_share": equity / shares}


# ----------------------------------------------------------------------------- reconciliation
def reconcile_values(methods: list[dict], values: dict, scen: dict, price: float | None, ke: float,
                     dps_12m: float, rules: dict) -> dict:
    used = [(m, values.get(m["id"])) for m in methods if m["status"] == "selected" and m["weight"] > 0 and values.get(m["id"])]
    if not used:
        return {"status": "no_defensible_valuation", "reason": "no selected method produced a value"}
    tot = sum(m["weight"] for m, _ in used)
    fair = sum(m["weight"] * v for m, v in used) / tot
    lo = min(scen["bear"]["value_per_share"], *(v for _, v in used))
    hi = max(scen["bull"]["value_per_share"], *(v for _, v in used))
    target = fair * (1 + ke) - dps_12m
    exp_ret = ((target + dps_12m) / price - 1) if price else None
    return {"status": "ok", "fair_value": fair, "range_low": lo, "range_high": hi, "target_12m": target,
            "expected_return": exp_ret, "components": [{"id": m["id"], "label": m["label"], "weight": m["weight"] / tot,
                                                        "value": v} for m, v in used],
            "dispersion": (hi - lo) / fair if fair else None}


def rating(exp_ret: float | None, ke: float, confidence: str, critical_fail: bool) -> dict:
    if critical_fail or exp_ret is None:
        return {"rating": "NO RATING", "why": "a critical quality gate failed or no sourced price: conclusion withheld"}
    if confidence == "LOW":
        return {"rating": "NO RATING", "why": "confidence LOW: evidence insufficient for a recommendation"}
    if exp_ret > ke + 0.10:
        r = "BUY"
    elif exp_ret < ke - 0.10:
        r = "SELL"
    else:
        r = "HOLD"
    return {"rating": r, "why": f"expected 12m total return {fmt_pct(exp_ret)} vs cost of equity {fmt_pct(ke)} "
                                f"(BUY > ke+10pp, SELL < ke-10pp)"}
