"""Evidence synthesis, independent critique and revision.

Deterministic critics inspect numbers and evidence (citations exist, quoted
numbers appear in the evidence pack, valuation sanity). The model critic
(when a provider is configured) challenges the narrative. One revision round
feeds critical/major findings back to the synthesis task."""
from __future__ import annotations

import json
import re
from typing import Any

from .util import fmt_pct

S = {"type": "string"}
SA = {"type": "array", "items": {"type": "string"}}

SYNTHESIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["view", "headline", "executive_summary", "thesis_pillars", "priced_in", "variant_perception",
                 "business_analysis", "industry_analysis", "financial_assessment", "forecast_rationale",
                 "governance_capital_allocation", "counter_thesis", "catalysts", "risks", "impact_matrix",
                 "upside_requirements", "downside_triggers", "falsification", "dominant_assumptions",
                 "evidence_gaps", "confidence", "confidence_rationale"],
    "properties": {
        "view": {"type": "string", "enum": ["POSITIVE", "NEUTRAL", "NEGATIVE", "INSUFFICIENT EVIDENCE"]},
        "headline": S, "executive_summary": SA,
        "thesis_pillars": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                      "required": ["title", "argument", "evidence_ids"],
                                                      "properties": {"title": S, "argument": S, "evidence_ids": SA}}},
        "priced_in": S, "variant_perception": S, "business_analysis": S, "industry_analysis": S,
        "financial_assessment": S, "forecast_rationale": S, "governance_capital_allocation": S,
        "counter_thesis": {"type": "object", "additionalProperties": False, "required": ["argument", "evaluation"],
                           "properties": {"argument": S, "evaluation": S}},
        "catalysts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "required": ["event", "timing", "dependency", "dated_by_evidence", "evidence_ids"],
                                                 "properties": {"event": S, "timing": S, "dependency": S,
                                                                "dated_by_evidence": {"type": "boolean"}, "evidence_ids": SA}}},
        "risks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["risk", "transmission", "thesis_breaker", "monitoring"],
                                             "properties": {"risk": S, "transmission": S, "thesis_breaker": {"type": "boolean"},
                                                            "monitoring": S}}},
        "impact_matrix": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                     "required": ["factor", "category", "materiality", "evidence_strength",
                                                                  "probability", "timeframe", "transmission", "model_variables",
                                                                  "monitoring", "evidence_ids"],
                                                     "properties": {"factor": S, "category": S,
                                                                    "materiality": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                                                                    "evidence_strength": {"type": "string", "enum": ["STRONG", "MODERATE", "WEAK", "FRAMEWORK ONLY"]},
                                                                    "probability": S, "timeframe": S, "transmission": S,
                                                                    "model_variables": SA, "monitoring": SA, "evidence_ids": SA}}},
        "upside_requirements": S, "downside_triggers": S, "falsification": SA, "dominant_assumptions": SA,
        "evidence_gaps": SA, "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]}, "confidence_rationale": S,
    },
}

CRITIQUE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["findings", "overall"],
    "properties": {
        "overall": S,
        "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                "required": ["area", "severity", "target", "problem", "required_revision"],
                                                "properties": {"area": {"type": "string", "enum": ["thesis", "accounting", "valuation", "citation", "presentation"]},
                                                               "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                                                               "target": S, "problem": S, "required_revision": S}}},
    },
}


# ----------------------------------------------------------------------------- evidence pack
def evidence_pack(ctx: dict) -> dict:
    """Compact, model-readable pack. Every figure carries the evidence id of its source."""
    st, drv = ctx["statements"], ctx["drivers"]
    fin_ev = ctx["evidence_ids"]["financials"]
    hist = []
    for p, r in zip(st["periods"], st["rows"]):
        hist.append({"period": p["label"], "period_end": p["end"], "evidence_id": fin_ev,
                     **{k: r.get(k) for k in ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
                                              "cfo", "capex", "fcf", "dividends_paid", "buybacks", "financial_assets",
                                              "total_debt", "total_equity", "sbc", "rnd")}})
    val = ctx["valuation"]
    return {
        "security": ctx["security"],
        "currency_units": f"{st['currency']}; financial lines in currency units (not millions) unless stated",
        "historical_financials": hist,
        "latest_interim": {"period_end": st.get("interim_end"), "ytd": st.get("ytd"), "ytd_prior_year": st.get("ytd_prior"),
                           "evidence_id": fin_ev},
        "driver_summary": {k: v for k, v in drv["summary"].items()},
        "quality_flags": drv["quality_flags"],
        "reconciliation": ctx["reconciliation"],
        "assumptions": {k: {"values": v["values"], "rationale": v["rationale"]} for k, v in ctx["assumptions"]["drivers"].items()},
        "market_inputs": ctx["market_inputs"],
        "valuation": {"methods": val["methods"], "dcf_base": val["dcf_base"], "scenarios": val["scenarios"],
                      "reverse_dcf": val["reverse_dcf"], "reconciled": val["reconciled"], "comps": val.get("comps_summary"),
                      "sensitivity_wacc_g": val["sensitivity"], "margin_growth_grid": val["margin_growth_grid"]},
        "sector_playbook": {k: ctx["playbook"].get(k) for k in ("id", "archetype", "kpis", "key_questions", "exposures", "note")},
        "document_excerpts": ctx["excerpts"],
        "research_plan": ctx["plan"],
    }


def build_prompt(pack: dict, prior_findings: list[dict] | None = None) -> str:
    out = ["Produce the investment synthesis for the security below.",
           "<evidence>", json.dumps(pack, default=str)[:600_000], "</evidence>"]
    if prior_findings:
        out += ["Independent critique findings on your previous draft. Revise to resolve each critical/major finding "
                "or explain in evidence_gaps why it cannot be resolved:", json.dumps(prior_findings, indent=1)]
    return "\n".join(out)


# ----------------------------------------------------------------------------- deterministic fallback
def deterministic_synthesis(ctx: dict) -> dict:
    """Used when no inference model is configured. States only computed facts;
    qualitative judgement is explicitly NOT provided."""
    v = ctx["valuation"]
    s = ctx["drivers"]["summary"]
    rd = v["reverse_dcf"]
    sc = v["scenarios"]
    nm = "(automated - no inference model configured)"
    exposures = ctx["playbook"].get("exposures", [])
    return {
        "view": "INSUFFICIENT EVIDENCE",
        "headline": f"Quantitative model complete; qualitative synthesis not performed {nm}",
        "executive_summary": [
            f"Base-case DCF value {v['dcf_base']['value_per_share']:.2f} per share vs reference price "
            f"{ctx['market_inputs']['price']['value']:.2f} ({fmt_pct(v['dcf_base']['upside'])})." if ctx['market_inputs']['price']['value'] else "No sourced market price.",
            f"Scenario range {sc['bear']['value_per_share']:.2f} (bear) to {sc['bull']['value_per_share']:.2f} (bull).",
            rd.get("narrative_growth", ""),
            "No investment view is expressed because evidence synthesis requires an inference model.",
        ],
        "thesis_pillars": [],
        "priced_in": rd.get("narrative_growth", "") + " " + rd.get("narrative_margin", ""),
        "variant_perception": f"Not assessed {nm}.",
        "business_analysis": f"Not synthesised {nm}. Filing excerpts are retained in the evidence pack.",
        "industry_analysis": f"Not synthesised {nm}.",
        "financial_assessment": (f"Revenue 3y CAGR {fmt_pct(s['revenue_cagr_3y'])}, 5y CAGR {fmt_pct(s['revenue_cagr_5y'])}; "
                                 f"EBIT margin 3y average {fmt_pct(s['ebit_margin']['avg3'])}; FCF conversion 3y average "
                                 f"{s['cash_conversion']['avg3']:.2f}x." if s['cash_conversion']['avg3'] is not None else ""),
        "forecast_rationale": "Rule-based assumptions per methodology/forecasting.yaml (see Assumptions sheet).",
        "governance_capital_allocation": f"Not assessed {nm}.",
        "counter_thesis": {"argument": f"Not developed {nm}.", "evaluation": "n/a"},
        "catalysts": [],
        "risks": [{"risk": e["factor"], "transmission": e["transmission"], "thesis_breaker": False,
                   "monitoring": "; ".join(e.get("monitoring", []))} for e in exposures],
        "impact_matrix": [{"factor": e["factor"], "category": e["category"], "materiality": "MEDIUM",
                           "evidence_strength": "FRAMEWORK ONLY", "probability": "not assessed", "timeframe": "not assessed",
                           "transmission": e["transmission"], "model_variables": e.get("model_variables", []),
                           "monitoring": e.get("monitoring", []), "evidence_ids": []} for e in exposures],
        "upside_requirements": rd.get("narrative_growth", ""),
        "downside_triggers": f"Bear case ({sc['bear']['value_per_share']:.2f}) assumes {sc['bear']['definition']}.",
        "falsification": [],
        "dominant_assumptions": ["WACC and terminal growth (see sensitivity)", "EBIT margin path", "Revenue growth path"],
        "evidence_gaps": ["Qualitative synthesis (business, industry, governance, catalysts) requires an inference model."],
        "confidence": "LOW",
        "confidence_rationale": f"Quantitative model only {nm}.",
        "_generated_by": "deterministic_fallback",
    }


# ----------------------------------------------------------------------------- deterministic critics
NUM = re.compile(r"(?<![A-Za-z0-9-])(\(?-?\d[\d,]*\.?\d*\)?)(\s?%|x\b|\s?(?:bn|billion|m|million))?")


def _numbers_in_pack(pack: Any) -> set[str]:
    vals: list[float] = []

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            vals.append(float(o))
        elif isinstance(o, dict):
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, str):
            for m in re.findall(r"-?\d[\d,]*\.?\d*", o):
                try:
                    vals.append(float(m.replace(",", "")))
                except ValueError:
                    pass
    walk(pack)
    out = set()
    for v in vals:
        for x in (v, v * 100, v / 1e6, v / 1e9, abs(v), abs(v) * 100, abs(v) / 1e6, abs(v) / 1e9):
            for dp in (0, 1, 2):
                out.add(f"{round(x, dp):.{dp}f}")
    return out


def critique_numbers_and_citations(synth: dict, pack: dict, evidence_ids: set[str]) -> list[dict]:
    findings = []
    allowed = _numbers_in_pack(pack)
    text_fields = []

    def collect(o, path):
        if isinstance(o, str):
            text_fields.append((path, o))
        elif isinstance(o, dict):
            for k, v in o.items():
                collect(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                collect(v, f"{path}[{i}]")
    collect({k: v for k, v in synth.items() if not k.startswith("_")}, "synthesis")
    for path, t in text_fields:
        for cid in re.findall(r"EV-\d{4}", t):
            if cid not in evidence_ids:
                findings.append({"area": "citation", "severity": "critical", "target": f"{path}: {cid}",
                                 "problem": "cites an evidence id that does not exist in the source register",
                                 "required_revision": "remove or replace with an existing evidence id"})
        for m in NUM.finditer(t):
            raw = m.group(1).strip("()").replace(",", "")
            try:
                x = float(raw)
            except ValueError:
                continue
            if abs(x) < 10 and not m.group(2):
                continue  # small integers (counts, years-of) are not audited
            if 1990 <= x <= 2100 and not m.group(2):
                continue  # years
            cands = {f"{round(x, dp):.{dp}f}" for dp in (0, 1, 2)}
            if not (cands & allowed):
                findings.append({"area": "citation", "severity": "minor", "target": f"{path}: '{m.group(0).strip()}'",
                                 "problem": "number not found in the evidence pack (possible recomputation or unsupported figure)",
                                 "required_revision": "quote the pack value or label as interpretation"})
    for key in ("thesis_pillars",):
        for i, p in enumerate(synth.get(key, [])):
            ids = p.get("evidence_ids") or []
            bad = [x for x in ids if x not in evidence_ids]
            if not ids:
                findings.append({"area": "thesis", "severity": "major", "target": f"{key}[{i}] '{p.get('title')}'",
                                 "problem": "thesis pillar cites no evidence", "required_revision": "cite evidence or label as interpretation"})
            for b in bad:
                findings.append({"area": "citation", "severity": "critical", "target": f"{key}[{i}]: {b}",
                                 "problem": "unknown evidence id", "required_revision": "fix citation"})
    for i, c in enumerate(synth.get("catalysts", [])):
        if c.get("dated_by_evidence") and not c.get("evidence_ids"):
            findings.append({"area": "thesis", "severity": "major", "target": f"catalysts[{i}] '{c.get('event')}'",
                             "problem": "catalyst claims an evidenced date but cites no evidence",
                             "required_revision": "cite the dated source or mark timing as estimated"})
    return findings


def critique_model_and_valuation(ctx: dict) -> list[dict]:
    f = []
    v = ctx["valuation"]
    d = v["dcf_base"]
    s = ctx["drivers"]["summary"]
    if d["tv_share"] is not None and d["tv_share"] > 0.80:
        f.append({"area": "valuation", "severity": "major", "target": f"TV share {fmt_pct(d['tv_share'])}",
                  "problem": "terminal value exceeds 80% of EV: value is dominated by long-run assumptions",
                  "required_revision": "disclose prominently; rely on sensitivity range rather than point value"})
    if d["terminal_g"] > (ctx["market_inputs"]["rf"]["value"] or 1):
        f.append({"area": "valuation", "severity": "critical", "target": "terminal growth",
                  "problem": "terminal growth exceeds the risk-free rate", "required_revision": "cap g at rf"})
    m_final = ctx["assumptions"]["drivers"]["ebit_margin"]["values"][-1]
    m5, sd = s["ebit_margin"]["avg5"], s["ebit_margin"]["stdev5"]
    if m5 is not None and sd and abs(m_final - m5) > 2 * max(sd, 0.005):
        f.append({"area": "accounting", "severity": "major", "target": f"final-year EBIT margin {fmt_pct(m_final)}",
                  "problem": f"more than 2 standard deviations from the 5y mean {fmt_pct(m5)}",
                  "required_revision": "justify with evidence or moderate the assumption"})
    a = ctx["assumptions"]["drivers"]
    if a["capex_pct"]["values"][-1] < a["dna_pct"]["values"][-1] and ctx["assumptions"]["drivers"]["growth"]["values"][-1] > 0:
        f.append({"area": "accounting", "severity": "major", "target": "terminal capex vs D&A",
                  "problem": "capex below D&A while growing", "required_revision": "raise capex or lower D&A"})
    t_final = a["tax_rate"]["values"][-1]
    stat = ctx["assumptions"]["statutory_tax"]
    if abs(t_final - stat) > 0.10:
        f.append({"area": "accounting", "severity": "major", "target": "tax rate", "problem": "outside statutory +/-10pp",
                  "required_revision": "bound tax rate"})
    sc = v["scenarios"]
    if not (sc["bear"]["value_per_share"] <= sc["base"]["value_per_share"] <= sc["bull"]["value_per_share"]):
        f.append({"area": "valuation", "severity": "critical", "target": "scenario ordering",
                  "problem": "bear <= base <= bull violated", "required_revision": "check scenario deltas"})
    if sc["stress"]["value_per_share"] > sc["bear"]["value_per_share"]:
        f.append({"area": "valuation", "severity": "minor", "target": "stress scenario",
                  "problem": "stress value above bear value", "required_revision": "review stress definition"})
    rec = v["reconciled"]
    if rec.get("dispersion") and rec["dispersion"] > 0.6:
        f.append({"area": "valuation", "severity": "minor", "target": "valuation range",
                  "problem": f"range width {fmt_pct(rec['dispersion'])} of fair value: low precision",
                  "required_revision": "present as a range; avoid point-estimate language"})
    beta = ctx["market_inputs"].get("beta") or {}
    if beta.get("value") is not None and not (0.4 <= beta["value"] <= 2.2):
        f.append({"area": "valuation", "severity": "major", "target": f"beta {beta['value']:.2f}",
                  "problem": "beta outside plausible bounds", "required_revision": "cross-check beta"})
    if beta.get("r2") is not None and beta["r2"] < 0.15:
        f.append({"area": "valuation", "severity": "minor", "target": f"beta R^2 {beta['r2']:.2f}",
                  "problem": "low explanatory power of the regression", "required_revision": "disclose; consider peer beta"})
    for c in ctx["reconciliation"]["checks"]:
        if c["pass"] is False:
            f.append({"area": "accounting", "severity": "major", "target": f"{c['check']} {c['period']}",
                      "problem": f"reconciliation difference {c.get('diff')}", "required_revision": "investigate; see discrepancy log"})
    return f
