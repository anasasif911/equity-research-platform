"""Driver-based forecast assumptions. Every value produced here is an ANALYST
ASSUMPTION derived by explicit, versioned rules (methodology/forecasting.yaml)
from reconciled history; each carries a rationale and the historical basis.
An inference model may later propose overrides, which are bounded by the
same guardrails and must cite evidence ids."""
from __future__ import annotations

from ..config import deep_merge
from ..util import fmt_pct


def _lin(a: float, b: float, n: int) -> list[float]:
    if n == 1:
        return [a]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _first(*vals, default=None):
    for v in vals:
        if v is not None:
            return v
    return default


def build_base(drv: dict, rules: dict, *, horizon: int, country: str, rf: float | None, short_rate: float | None,
               last_row: dict, terminal_cap: float = 0.03, statutory: float | None = None, kd_floor: float | None = None,
               max_run_rate: float | None = None, statutory_source: str | None = None) -> dict:
    s = drv["summary"]
    g_rules = rules["drivers"]
    out: dict[str, dict] = {}

    # ---- revenue growth
    run_rate = None
    parts = [(s["revenue_cagr_3y"], 0.5), (s["revenue_growth"]["last"], 0.3), (s["revenue_cagr_5y"], 0.2)]
    w = sum(wt for v, wt in parts if v is not None)
    if w:
        run_rate = sum(v * wt for v, wt in parts if v is not None) / w
    run_rate = run_rate if run_rate is not None else 0.03
    run_rate_note = ""
    if max_run_rate is not None and run_rate > max_run_rate:
        run_rate_note = (f" Historical run-rate {fmt_pct(run_rate)} capped at {fmt_pct(max_run_rate)} (sector playbook: cyclical "
                         f"business; recent growth not extrapolated).")
        run_rate = max_run_rate
    g_term = min(terminal_cap, rf if rf is not None else terminal_cap)
    g1 = s.get("ytd_revenue_growth")
    if g1 is not None:
        start, basis = g1, f"Year 1 anchored on reported year-to-date growth of {fmt_pct(g1)} vs prior-year period"
    else:
        start, basis = run_rate, f"Year 1 = weighted run-rate (50% 3y CAGR, 30% last year, 20% 5y CAGR) = {fmt_pct(run_rate)}"
    lo, hi = g_rules["revenue_growth"]["guardrails"]["min"], g_rules["revenue_growth"]["guardrails"]["max"]
    end = g_term + 0.005 if run_rate >= g_term else (run_rate + g_term) / 2
    if g1 is not None and horizon >= 2:
        path = [g1] + _lin(run_rate, end, horizon - 1)   # year 1 = YTD run-rate, then fade from history
    else:
        path = _lin(run_rate, end, horizon)
    path = [_clip(x, lo, hi) for x in path]
    out["growth"] = {"values": path, "label": "Revenue growth", "fmt": "percent",
                     "rationale": f"{basis}; then fading linearly from the run-rate {fmt_pct(run_rate)} to {fmt_pct(end)} by the final year "
                                  f"(terminal growth {fmt_pct(g_term)}). Historical 3y CAGR {fmt_pct(s['revenue_cagr_3y'])}, "
                                  f"5y CAGR {fmt_pct(s['revenue_cagr_5y'])}." + run_rate_note}

    # ---- EBIT margin
    m3, m5 = s["ebit_margin"]["avg3"], s["ebit_margin"]["avg5"]
    m_start = _first(s.get("ytd_ebit_margin"), m3, s["ebit_margin"]["last"], default=0.10)
    m_end = m_start + 0.5 * ((_first(m5, m_start)) - m_start)
    out["ebit_margin"] = {"values": _lin(m_start, m_end, horizon), "label": "EBIT margin", "fmt": "percent",
                          "rationale": f"Starts at {'YTD' if s.get('ytd_ebit_margin') is not None else '3y average'} margin "
                                       f"{fmt_pct(m_start)}; fades halfway to the 5y average {fmt_pct(m5)} by the final year "
                                       f"(mean reversion absent evidence of structural change)."}

    # ---- tax
    stat = statutory if statutory is not None else g_rules["tax_rate"]["statutory_default"].get(country, g_rules["tax_rate"]["statutory_default"]["DEFAULT"])
    t3 = _first(s["tax_rate"]["median3"], stat)
    t3 = _clip(t3, stat - 0.10, stat + 0.10)
    t_end = (t3 + stat) / 2
    out["tax_rate"] = {"values": _lin(t3, t_end, horizon), "label": "Effective tax rate", "fmt": "percent",
                       "rationale": f"3y median effective rate {fmt_pct(t3)} (bounded to statutory {fmt_pct(stat)} +/-10pp), "
                                    f"converging to the midpoint with statutory ({fmt_pct(t_end)}) as a prudence assumption "
                                    f"on global minimum-tax and rate risk." + (f" Statutory rate source: {statutory_source}." if statutory_source else "")}

    # ---- reinvestment
    capex = _first(s["capex_pct_revenue"]["avg3"], 0.04)
    dna = _first(s["dna_pct_revenue"]["avg3"], capex)
    dna_end = min(dna, capex * 0.9)
    out["capex_pct"] = {"values": [capex] * horizon, "label": "Capex / revenue", "fmt": "percent",
                        "rationale": f"Held at the 3y average {fmt_pct(capex)}."}
    out["dna_pct"] = {"values": _lin(dna, dna_end, horizon), "label": "D&A / revenue", "fmt": "percent",
                      "rationale": f"3y average {fmt_pct(dna)} converging to <= 90% of capex ({fmt_pct(dna_end)}) so a growing "
                                   f"business keeps net investment positive."}
    nwc = _first(s["nwc_pct_revenue"]["avg3"], 0.0)
    out["nwc_pct"] = {"values": [nwc] * horizon, "label": "Operating NWC / revenue", "fmt": "percent",
                      "rationale": f"Held at the 3y average {fmt_pct(nwc)}; a negative value means suppliers/customers fund operations."}

    # ---- distributions
    payout = _clip(_first(s["payout_ratio"]["avg3"], 0.0), 0.0, 1.0)
    out["payout"] = {"values": [payout] * horizon, "label": "Dividend payout (of net income)", "fmt": "percent",
                     "rationale": f"3y average payout {fmt_pct(payout)}."}
    bb_hist = s["buyback_pct_ni"]["avg3"]
    bb = _clip(_first(bb_hist, 0.0) / max(1e-9, 1 - payout) if bb_hist is not None else 0.0, 0.0, 1.0)
    out["buyback_pct"] = {"values": [bb] * horizon, "label": "Buybacks (% of FCF after dividends)", "fmt": "percent",
                          "rationale": f"Historical buybacks were {fmt_pct(bb_hist)} of net income (3y avg); modelled as {fmt_pct(bb)} of free cash "
                                       f"flow after dividends (capped at 100%). Buybacks at expected market prices are value-neutral "
                                       f"in the DCF; they affect forecast EPS and net cash only."}

    # ---- financing
    cy = short_rate if short_rate is not None else (rf if rf is not None else 0.03)
    out["cash_yield"] = {"values": [cy] * horizon, "label": "Yield on cash & investments", "fmt": "percent",
                         "rationale": f"Short-term Treasury yield {fmt_pct(cy)} held flat (sourced market rate used as assumption)."}
    debt_avg = last_row.get("total_debt") or 0
    ie = last_row.get("interest_expense")
    kd = (ie / debt_avg) if (ie and debt_avg) else ((rf or 0.04) + 0.01)
    kd = _clip(kd, 0.0, 0.25)
    kd_note = ""
    if kd_floor is not None and kd < kd_floor:
        kd_note = f" Floored at the government yield {fmt_pct(kd_floor)} (a corporate borrower cannot fund below the sovereign)."
        kd = kd_floor
    out["kd"] = {"values": [kd] * horizon, "label": "Interest rate on gross debt", "fmt": "percent",
                 "rationale": ("Effective rate = last-year interest expense / gross debt "
                               if ie and debt_avg else "Risk-free + 1.0pp assumed spread (interest expense not separately tagged) ")
                              + f"= {fmt_pct(kd)}." + kd_note}
    out["debt_change"] = {"values": [0.0] * horizon, "label": "Net debt issuance / (repayment)", "fmt": "millions",
                          "rationale": "Gross debt held flat (refinancing at maturity) absent disclosed changes."}
    return {"drivers": out, "terminal_growth": g_term, "statutory_tax": stat,
            "terminal_rationale": f"Terminal nominal growth {fmt_pct(g_term)} = min(10y risk-free {fmt_pct(rf)}, {fmt_pct(terminal_cap)} cap)."}


def scenario_set(base: dict, rules: dict, playbook: dict) -> dict:
    sc_rules = deep_merge(rules["scenarios"], playbook.get("scenario_overrides") or {})
    out = {}
    for name in ("base", "bull", "bear", "stress"):
        r = sc_rules[name]
        d = {}
        for k, v in base["drivers"].items():
            vals = list(v["values"])
            if k == "growth":
                k_years = r.get("revenue_shock_years") or len(vals)
                vals = [x + (r.get("revenue_growth_delta", 0) if i < k_years else 0) for i, x in enumerate(vals)]
            if k == "ebit_margin":
                vals = [x + r.get("margin_delta", 0) for x in vals]
            d[k] = vals
        out[name] = {"label": r["label"], "probability": r.get("probability", 0), "drivers": d,
                     "wacc_delta": r.get("wacc_delta", 0.0), "terminal_g_delta": r.get("terminal_g_delta", 0.0),
                     "definition": {k: r.get(k) for k in ("revenue_growth_delta", "margin_delta", "wacc_delta",
                                                          "terminal_g_delta", "revenue_shock_years") if r.get(k) is not None}}
    return out


def apply_overrides(base: dict, overrides: dict, rules: dict) -> list[dict]:
    """Analyst/model overrides: {driver: {"values": [...] | "first"/"last": x, "rationale": str, "evidence_ids": [...]}}.
    Bounded by the same guardrails; every override is labelled in the rationale."""
    applied = []
    g = rules["drivers"]
    for k, ov in (overrides or {}).items():
        if k == "terminal_growth":
            base["terminal_growth"] = float(ov["value"])
            base["terminal_rationale"] = f"ANALYST OVERRIDE: {ov['rationale']} [{', '.join(ov.get('evidence_ids', []))}]"
            applied.append({"driver": k, **ov})
            continue
        if k not in base["drivers"]:
            continue
        n = len(base["drivers"][k]["values"])
        vals = ov.get("values") or _lin(float(ov["first"]), float(ov["last"]), n)
        vals = [float(x) for x in vals][:n] + [float(vals[-1])] * max(0, n - len(vals))
        guard = {"growth": g["revenue_growth"]["guardrails"], "ebit_margin": g["operating_margin"]["guardrails"]}.get(k)
        if guard:
            vals = [_clip(v, guard["min"], guard["max"]) for v in vals]
        base["drivers"][k]["values"] = vals
        base["drivers"][k]["rationale"] = (f"ANALYST OVERRIDE: {ov['rationale']} [{', '.join(ov.get('evidence_ids', []))}]. "
                                           f"(Rule-based value replaced: {base['drivers'][k]['rationale']})")
        applied.append({"driver": k, **ov})
    return applied
