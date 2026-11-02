"""Builds the integrated three-statement + valuation model specification.

Forecast balance sheet is presented in invested-capital (condensed) form:
financial assets + operating NWC + net PP&E + other net operating assets
(held flat) = gross debt + total equity (incl. NCI). The identity holds by
construction only if every flow is linked correctly, so the BS check row is
a genuine integrity test of the schedules. The detailed reported balance
sheet (and its own identity check) is on Hist_Data.

Units: currency millions except per-share values and share counts (millions).
"""
from __future__ import annotations

import datetime as dt

from .sheetmodel import ModelSpec, Row, Scalar

SHEETS = ["Hist_Data", "Assumptions", "IS", "BS", "CF", "Schedules", "WACC", "DCF", "Sensitivity", "Checks"]
DRIVER_ORDER = ["growth", "ebit_margin", "tax_rate", "dna_pct", "capex_pct", "nwc_pct", "payout", "buyback_pct",
                "cash_yield", "kd", "debt_change"]
SCEN = ["base", "bull", "bear", "stress"]
M = 1e6  # model in millions

HIST_ITEMS = [
    ("revenue", "Revenue"), ("cogs", "Cost of revenue"), ("gross_profit", "Gross profit"), ("rnd", "R&D"),
    ("sga", "SG&A"), ("operating_income", "Operating income (EBIT)"), ("dna", "Depreciation & amortisation"),
    ("sbc", "Stock-based compensation"), ("interest_expense", "Interest expense"), ("pretax_income", "Pre-tax income"),
    ("income_tax", "Income tax"), ("net_income", "Net income"), ("eps_diluted", "Diluted EPS (reported)"),
    ("shares_diluted_weighted", "Diluted weighted shares (m)"),
    ("cash", "Cash & equivalents"), ("short_term_investments", "Short-term investments"),
    ("long_term_investments", "Long-term investments"), ("financial_assets", "Financial assets (cash + investments)"),
    ("receivables", "Trade receivables"), ("inventory", "Inventory"), ("total_current_assets", "Total current assets"),
    ("ppe_net", "Net PP&E"), ("total_assets", "Total assets"), ("accounts_payable", "Accounts payable"),
    ("total_current_liabilities", "Total current liabilities"), ("total_debt", "Gross debt"),
    ("operating_lease_liabilities", "Operating lease liabilities"), ("total_liabilities", "Total liabilities"),
    ("total_equity", "Shareholders' equity"), ("nci", "Non-controlling interests"),
    ("operating_nwc", "Operating NWC"), ("cfo", "Cash from operations (reported)"), ("capex", "Capital expenditure"),
    ("acquisitions", "Acquisitions"), ("dividends_paid", "Dividends paid"), ("buybacks", "Share repurchases"),
    ("cfi", "Cash from investing"), ("cff", "Cash from financing"), ("fx_effect", "FX effect on cash"),
    ("cash_end_cf", "Cash (cash-flow statement basis)"),
]
PER_SHARE = {"eps_diluted"}
SHARES = {"shares_diluted_weighted"}


def excel_serial(d: dt.date) -> float:
    return float((d - dt.date(1899, 12, 30)).days)


def _hv(st: dict, item: str, idx: list[int]) -> list:
    out = []
    for i in idx:
        v = st["rows"][i].get(item)
        if v is None:
            out.append(None)
        elif item in PER_SHARE:
            out.append(v)
        else:
            out.append(v / M)
    return out


def build_spec(*, st: dict, drv: dict, scenarios: dict, base_assump: dict, market: dict, coc: dict,
               horizon: int, hist_years: int = 6, scenario_index: int = 1) -> ModelSpec:
    periods = st["periods"]
    idx = list(range(max(0, len(periods) - hist_years), len(periods)))
    hist_labels = [periods[i]["label"] for i in idx]
    last_end = dt.date.fromisoformat(periods[-1]["end"])
    fc_ends = [dt.date(last_end.year + k, last_end.month, min(last_end.day, 28)) for k in range(1, horizon + 1)]
    fc_labels = [f"FY{d.year if d.month > 3 else d.year - 1}E" for d in fc_ends]
    spec = ModelSpec(hist_labels=hist_labels, fc_labels=fc_labels, sheets=SHEETS)
    spec.meta = {"fc_ends": [d.isoformat() for d in fc_ends], "hist_idx": idx}
    H, N = spec.H, spec.N

    # ------------------------------------------------------------------ Hist_Data
    for item, label in HIST_ITEMS:
        fmt = "per_share" if item in PER_SHARE else "millions"
        spec.add(Row("Hist_Data", item, label, "per share" if item in PER_SHARE else "m", hist=_hv(st, item, idx),
                     fmt=fmt, style="input", source="SEC XBRL companyfacts; cell-level provenance on 'Provenance'"))
    spec.add(Row("Hist_Data", "bs_identity_diff", "Check: assets - liabilities - equity - NCI", "m",
                 hist="{r:total_assets}-{r:total_liabilities}-{r:total_equity}-{r:nci}", style="check"))

    # ------------------------------------------------------------------ Assumptions
    s = drv["series"]
    hist_driver = {
        "growth": s["revenue_growth"], "ebit_margin": s["ebit_margin"], "tax_rate": s["tax_rate"],
        "dna_pct": s["dna_pct_revenue"], "capex_pct": s["capex_pct_revenue"], "nwc_pct": s["nwc_pct_revenue"],
        "payout": s["payout_ratio"],
    }
    for k in DRIVER_ORDER:
        meta = base_assump["drivers"][k]
        fmt = "millions" if k == "debt_change" else "percent"
        spec.add(Row("Assumptions", f"h_{k}", meta["label"], "", style="header", note=meta["rationale"]))
        for sc in SCEN:
            spec.add(Row("Assumptions", f"{k}_{sc}", f"  {scenarios[sc]['label']}", "", fc=list(scenarios[sc]["drivers"][k]),
                         fmt=fmt, style="input"))
        hv = [hist_driver[k][i] for i in idx] if k in hist_driver else None
        spec.add(Row("Assumptions", k, f"  Live ({meta['label']})", "", hist=hv,
                     fc="CHOOSE({s:scenario}," + ",".join(f"{{r:{k}_{sc}}}" for sc in SCEN) + ")", fmt=fmt, style="total",
                     note="Selected scenario feeds the model"))
    spec.scalar(
        Scalar("Assumptions", "scenario", "Active scenario (1 Base, 2 Bull, 3 Bear, 4 Stress)", scenario_index, fmt="int", style="input"),
        Scalar("Assumptions", "terminal_g_base", "Terminal growth - base", base_assump["terminal_growth"], fmt="percent",
               style="input", note=base_assump["terminal_rationale"]),
        *[Scalar("Assumptions", f"g_delta_{sc}", f"Terminal growth delta - {scenarios[sc]['label']}", scenarios[sc]["terminal_g_delta"],
                 fmt="percent", style="input") for sc in SCEN],
        *[Scalar("Assumptions", f"wacc_delta_{sc}", f"WACC delta - {scenarios[sc]['label']}", scenarios[sc]["wacc_delta"],
                 fmt="percent", style="input") for sc in SCEN],
        Scalar("Assumptions", "terminal_g", "Terminal growth - live",
               "{s:terminal_g_base}+CHOOSE({s:scenario}," + ",".join(f"{{s:g_delta_{sc}}}" for sc in SCEN) + ")",
               fmt="percent", style="total"),
        Scalar("Assumptions", "roic_hist", "Historical ROIC (3y average, reported basis)", coc["roic_hist"], fmt="percent",
               style="input", note="Computed from reconciled history (NOPAT / average invested capital)"),
    )

    # ------------------------------------------------------------------ WACC
    spec.scalar(
        Scalar("WACC", "rf", "Risk-free rate (10y government)", coc["rf"], fmt="percent", style="input", source=coc["rf_source"]),
        Scalar("WACC", "erp", "Equity risk premium", coc["erp"], fmt="percent", style="input", source=coc["erp_source"]),
        Scalar("WACC", "crp", "Country risk premium", coc.get("crp", 0.0), fmt="percent", style="input",
               source=coc.get("crp_source", "Mature market: none applied")),
        Scalar("WACC", "beta_raw", "Raw regression beta", coc["beta_raw"], fmt="ratio", style="input", source=coc["beta_source"]),
        Scalar("WACC", "beta_adj", "Adjusted beta (Blume: 0.67 x raw + 0.33)", "0.67*{s:beta_raw}+0.33", fmt="ratio"),
        Scalar("WACC", "ke", "Cost of equity (CAPM)", "{s:rf}+{s:beta_adj}*{s:erp}+{s:crp}", fmt="percent", style="total"),
        Scalar("WACC", "kd_pre", "Pre-tax cost of debt", coc["kd_pre"], fmt="percent", style="input", source=coc["kd_source"]),
        Scalar("WACC", "tax_marginal", "Marginal tax rate (debt shield)", coc["tax_marginal"], fmt="percent", style="input",
               source="Statutory rate (methodology/forecasting.yaml)"),
        Scalar("WACC", "kd_post", "After-tax cost of debt", "{s:kd_pre}*(1-{s:tax_marginal})", fmt="percent"),
        Scalar("WACC", "price", "Reference share price", market["price"], fmt="per_share", style="input", source=market["price_source"]),
        Scalar("WACC", "diluted_shares", "Diluted shares (m)", market["diluted_shares"] / M, fmt="millions_1dp", style="input",
               source=market["shares_source"]),
        Scalar("WACC", "equity_mv", "Market value of equity", "{s:price}*{s:diluted_shares}", fmt="millions"),
        Scalar("WACC", "debt_bv", "Gross debt (book, proxy for market)", market["debt"] / M, fmt="millions", style="input",
               source=market["bs_source"]),
        Scalar("WACC", "we", "Weight of equity", "{s:equity_mv}/({s:equity_mv}+{s:debt_bv})", fmt="percent"),
        Scalar("WACC", "wd", "Weight of debt", "1-{s:we}", fmt="percent"),
        Scalar("WACC", "wacc_base", "WACC (base)", "{s:we}*{s:ke}+{s:wd}*{s:kd_post}", fmt="percent", style="total"),
        Scalar("WACC", "wacc", "WACC (live scenario)",
               "{s:wacc_base}+CHOOSE({s:scenario}," + ",".join(f"{{s:wacc_delta_{sc}}}" for sc in SCEN) + ")",
               fmt="percent", style="total"),
        Scalar("WACC", "ronic", "Terminal return on new invested capital",
               "MAX({s:wacc},{s:wacc}+0.5*(MIN({s:roic_hist},0.4)-{s:wacc}))", fmt="percent",
               note="RONIC = WACC + 50% x (min(ROIC, 40%) - WACC), floored at WACC"),
        Scalar("WACC", "valuation_date", "Valuation date", excel_serial(market["valuation_date"]), fmt="date", style="input"),
        Scalar("WACC", "bs_date", "Balance-sheet date for EV bridge", excel_serial(market["bs_date"]), fmt="date", style="input",
               source=market["bs_source"]),
    )

    # ------------------------------------------------------------------ IS (block 1)
    def hd(item):
        return "{r:Hist_Data.%s}" % item
    growth_hist = [s["revenue_growth"][i] for i in idx]
    margin_hist = [s["ebit_margin"][i] for i in idx]
    spec.add(
        Row("IS", "revenue", "Revenue", "m", hist=hd("revenue"), fc="{r:revenue@-1}*(1+{r:Assumptions.growth})", style="total"),
        Row("IS", "growth", "  growth", "%", hist=growth_hist, fc="{r:revenue}/{r:revenue@-1}-1", fmt="percent", style="memo"),
        Row("IS", "ebit", "Operating income (EBIT)", "m", hist=hd("operating_income"), fc="{r:revenue}*{r:Assumptions.ebit_margin}", style="total"),
        Row("IS", "ebit_margin", "  EBIT margin", "%", hist=margin_hist, fc="{r:ebit}/{r:revenue}", fmt="percent", style="memo"),
        Row("IS", "dna", "  memo: D&A (in EBIT)", "m", hist=hd("dna"), fc="{r:revenue}*{r:Assumptions.dna_pct}", style="memo"),
        Row("IS", "ebitda", "EBITDA", "m", hist="{r:ebit}+{r:dna}", fc="{r:ebit}+{r:dna}"),
        Row("IS", "interest_income", "Interest income on cash & investments", "m", fc="{r:BS.fin_assets@-1}*{r:Assumptions.cash_yield}"),
        Row("IS", "interest_expense", "Interest expense", "m", hist=hd("interest_expense"), fc="{r:Schedules.debt@-1}*{r:Assumptions.kd}"),
        Row("IS", "nonop", "Net non-operating income", "m", hist=f"{hd('pretax_income')}-{hd('operating_income')}",
            fc="{r:interest_income}-{r:interest_expense}", note="Historical: reported pre-tax minus EBIT"),
        Row("IS", "pretax", "Pre-tax income", "m", hist=hd("pretax_income"), fc="{r:ebit}+{r:nonop}"),
        Row("IS", "tax", "Income tax", "m", hist=hd("income_tax"), fc="{r:pretax}*{r:Assumptions.tax_rate}"),
        Row("IS", "net_income", "Net income", "m", hist=hd("net_income"), fc="{r:pretax}-{r:tax}", style="total"),
    )
    # ------------------------------------------------------------------ Schedules (block 1)
    nwc_h = _hv(st, "operating_nwc", idx)
    dnwc_h = [None] + [(b - a) if (a is not None and b is not None) else None for a, b in zip(nwc_h, nwc_h[1:])]
    spec.add(
        Row("Schedules", "h_wc", "Working capital", style="header"),
        Row("Schedules", "year_index", "Forecast year index", "#", fc=list(range(1, N + 1)), fmt="int", style="memo"),
        Row("Schedules", "nwc", "Operating NWC", "m", hist=hd("operating_nwc"), fc="{r:IS.revenue}*{r:Assumptions.nwc_pct}"),
        Row("Schedules", "delta_nwc", "Increase / (decrease) in NWC", "m", hist=dnwc_h, fc="{r:nwc}-{r:nwc@-1}"),
        Row("Schedules", "h_capex", "Capital expenditure and PP&E", style="header"),
        Row("Schedules", "capex", "Capex", "m", hist="-" + hd("capex"), fc="{r:IS.revenue}*{r:Assumptions.capex_pct}"),
        Row("Schedules", "ppe", "Net PP&E (roll-forward: open + capex - D&A)", "m", hist=hd("ppe_net"),
            fc="{r:ppe@-1}+{r:capex}-{r:IS.dna}", note="D&A includes amortisation of intangibles (simplification, disclosed)"),
        Row("Schedules", "h_debt", "Debt", style="header"),
        Row("Schedules", "debt_change", "Net issuance / (repayment)", "m", fc="{r:Assumptions.debt_change}"),
        Row("Schedules", "debt", "Gross debt (closing)", "m", hist=hd("total_debt"), fc="{r:debt@-1}+{r:debt_change}", style="total"),
    )
    # ------------------------------------------------------------------ CF (block 1)
    spec.add(
        Row("CF", "net_income", "Net income", "m", hist="{r:IS.net_income}", fc="{r:IS.net_income}"),
        Row("CF", "dna", "Depreciation & amortisation", "m", hist="{r:IS.dna}", fc="{r:IS.dna}"),
        Row("CF", "delta_nwc", "(Increase) / decrease in NWC", "m", hist=[(-v if v is not None else None) for v in dnwc_h],
            fc="-{r:Schedules.delta_nwc}"),
        Row("CF", "cfo", "Cash from operations", "m", hist=hd("cfo"), fc="{r:net_income}+{r:dna}+{r:delta_nwc}", style="total",
            note="Forecast CFO excludes the SBC add-back: SBC is treated as a cash-equivalent expense (no double count with dilution)"),
        Row("CF", "capex", "Capital expenditure", "m", hist=hd("capex"), fc="-{r:Schedules.capex}"),
        Row("CF", "fcf", "Free cash flow (CFO - capex)", "m", hist="{r:cfo}+{r:capex}", fc="{r:cfo}+{r:capex}", style="total"),
    )
    # ------------------------------------------------------------------ Schedules (block 2: distributions, shares)
    spec.add(
        Row("Schedules", "h_dist", "Distributions and share count", style="header"),
        Row("Schedules", "dividends", "Dividends", "m", hist="-" + hd("dividends_paid"), fc="{r:IS.net_income}*{r:Assumptions.payout}"),
        Row("Schedules", "buybacks", "Share repurchases", "m", hist="-" + hd("buybacks"),
            fc="MAX(0,{r:CF.cfo}-{r:capex}-{r:dividends})*{r:Assumptions.buyback_pct}"),
        Row("Schedules", "repurchase_price", "Assumed repurchase price (price grown at cost of equity)", "per share",
            fc="{s:price}*(1+{s:ke})^({r:year_index}-0.5)", fmt="per_share", style="memo"),
        Row("Schedules", "shares", "Diluted shares (m, closing)", "m", hist=hd("shares_diluted_weighted"),
            fc="{r:shares@-1}-{r:buybacks}/{r:repurchase_price}", fmt="millions_1dp",
            note="Historical: diluted weighted average; forecast: prior less repurchased shares (net of grants)"),
    )
    # ------------------------------------------------------------------ CF (block 2)
    spec.add(
        Row("CF", "dividends", "Dividends paid", "m", hist=hd("dividends_paid"), fc="-{r:Schedules.dividends}"),
        Row("CF", "buybacks", "Share repurchases", "m", hist=hd("buybacks"), fc="-{r:Schedules.buybacks}"),
        Row("CF", "debt_change", "Net debt issuance / (repayment)", "m", fc="{r:Schedules.debt_change}"),
        Row("CF", "net_change", "Change in financial assets", "m", fc="{r:fcf}+{r:dividends}+{r:buybacks}+{r:debt_change}", style="total"),
    )
    # ------------------------------------------------------------------ BS
    spec.add(
        Row("BS", "fin_assets", "Cash & investments", "m", hist=hd("financial_assets"), fc="{r:fin_assets@-1}+{r:CF.net_change}"),
        Row("BS", "nwc", "Operating NWC", "m", hist=hd("operating_nwc"), fc="{r:Schedules.nwc}"),
        Row("BS", "ppe", "Net PP&E", "m", hist=hd("ppe_net"), fc="{r:Schedules.ppe}"),
        Row("BS", "other_net", "Other net operating assets / (liabilities)", "m",
            hist=f"{hd('total_equity')}+{hd('nci')}+{hd('total_debt')}-{hd('financial_assets')}-{hd('operating_nwc')}-{hd('ppe_net')}",
            fc="{r:other_net@-1}", note="Goodwill, intangibles, deferred tax, other assets/liabilities; held flat (assumption)"),
        Row("BS", "net_assets", "Total net assets", "m", hist="{r:fin_assets}+{r:nwc}+{r:ppe}+{r:other_net}",
            fc="{r:fin_assets}+{r:nwc}+{r:ppe}+{r:other_net}", style="total"),
        Row("BS", "debt", "Gross debt", "m", hist=hd("total_debt"), fc="{r:Schedules.debt}"),
        Row("BS", "equity", "Total equity (incl. NCI)", "m", hist=f"{hd('total_equity')}+{hd('nci')}",
            fc="{r:equity@-1}+{r:IS.net_income}-{r:Schedules.dividends}-{r:Schedules.buybacks}"),
        Row("BS", "capital", "Total capital", "m", hist="{r:debt}+{r:equity}", fc="{r:debt}+{r:equity}", style="total"),
        Row("BS", "check", "Check: net assets - capital (must be 0)", "m", hist="{r:net_assets}-{r:capital}",
            fc="{r:net_assets}-{r:capital}", style="check"),
    )
    # ------------------------------------------------------------------ IS (block 2: per share)
    spec.add(
        Row("IS", "shares_avg", "Diluted shares, average (m)", "m", hist=hd("shares_diluted_weighted"),
            fc="({r:Schedules.shares}+{r:Schedules.shares@-1})/2", fmt="millions_1dp"),
        Row("IS", "eps", "Diluted EPS", "per share", hist="{r:net_income}/{r:shares_avg}", fc="{r:net_income}/{r:shares_avg}",
            fmt="per_share", style="total"),
        Row("IS", "dps", "Dividend per share", "per share", hist="{r:Schedules.dividends}/{r:shares_avg}",
            fc="{r:Schedules.dividends}/{r:shares_avg}", fmt="per_share"),
    )
    # ------------------------------------------------------------------ DCF
    spec.add(
        Row("DCF", "ebit", "EBIT", "m", fc="{r:IS.ebit}"),
        Row("DCF", "tax_rate", "Tax rate on EBIT", "%", fc="{r:Assumptions.tax_rate}", fmt="percent"),
        Row("DCF", "nopat", "NOPAT", "m", fc="{r:ebit}*(1-{r:tax_rate})", style="total"),
        Row("DCF", "dna", "+ D&A", "m", fc="{r:IS.dna}"),
        Row("DCF", "capex", "- Capex", "m", fc="-{r:Schedules.capex}"),
        Row("DCF", "delta_nwc", "- Increase in NWC", "m", fc="-{r:Schedules.delta_nwc}"),
        Row("DCF", "fcff", "Free cash flow to firm (FCFF)", "m", fc="{r:nopat}+{r:dna}+{r:capex}+{r:delta_nwc}", style="total"),
        Row("DCF", "period_end", "Fiscal period end", "date", fc=[excel_serial(d) for d in fc_ends], fmt="date", style="input"),
        Row("DCF", "frac", "Fraction of year after balance-sheet date", "x",
            fc="MAX(0,MIN(1,({r:period_end}-{s:bs_date})/365.25))", fmt="ratio"),
        Row("DCF", "time", "Discount period (mid-point, years from valuation date)", "yrs",
            fc="({r:period_end}-{r:frac}*365.25/2-{s:valuation_date})/365.25", fmt="ratio"),
        Row("DCF", "fcff_incl", "FCFF attributable to valuation", "m", fc="{r:fcff}*{r:frac}"),
        Row("DCF", "df", "Discount factor", "x", fc="1/(1+{s:wacc})^{r:time}", fmt="ratio4"),
        Row("DCF", "pv", "Present value of FCFF", "m", fc="{r:fcff_incl}*{r:df}", style="total"),
    )
    k12 = _next12_index(fc_ends, market["valuation_date"])
    spec.scalar(
        Scalar("DCF", "sum_pv", "Sum of PV of explicit FCFF", "SUM({range:DCF.pv})"),
        Scalar("DCF", "nopat_t1", "Terminal-year NOPAT (N+1)", "{last:DCF.nopat}*(1+{s:terminal_g})"),
        Scalar("DCF", "reinvest_rate", "Terminal reinvestment rate (g / RONIC)", "{s:terminal_g}/{s:ronic}", fmt="percent"),
        Scalar("DCF", "fcff_t1", "Terminal-year FCFF", "{s:nopat_t1}*(1-{s:reinvest_rate})"),
        Scalar("DCF", "tv", "Terminal value at end of horizon", "{s:fcff_t1}/({s:wacc}-{s:terminal_g})"),
        Scalar("DCF", "time_tv", "Years from valuation date to horizon end", "({last:DCF.period_end}-{s:valuation_date})/365.25", fmt="ratio"),
        Scalar("DCF", "pv_tv", "PV of terminal value (mid-year consistent)", "{s:tv}*(1+{s:wacc})^0.5/(1+{s:wacc})^{s:time_tv}"),
        Scalar("DCF", "ev", "Enterprise value", "{s:sum_pv}+{s:pv_tv}", style="total"),
        Scalar("DCF", "tv_share", "Terminal value share of EV", "{s:pv_tv}/{s:ev}", fmt="percent"),
        Scalar("DCF", "fin_assets_latest", "+ Cash & investments (latest balance sheet)", market["fin_assets"] / M, style="input",
               source=market["bs_source"]),
        Scalar("DCF", "debt_latest", "- Gross debt (latest balance sheet)", market["debt"] / M, style="input", source=market["bs_source"]),
        Scalar("DCF", "nci_latest", "- Non-controlling interests", market["nci"] / M, style="input", source=market["bs_source"]),
        Scalar("DCF", "lease_adj", "- Lease liabilities treated as debt (IFRS 16 only)", market["lease_adj"] / M, style="input",
               note=market["lease_note"]),
        Scalar("DCF", "equity_value", "Equity value",
               "{s:ev}+{s:fin_assets_latest}-{s:debt_latest}-{s:nci_latest}-{s:lease_adj}", style="total"),
        Scalar("DCF", "value_per_share", "Intrinsic value per share (today)", "{s:equity_value}/{s:diluted_shares}",
               fmt="per_share", style="total"),
        Scalar("DCF", "upside", "Upside / (downside) vs reference price", "{s:value_per_share}/{s:price}-1", fmt="percent"),
        Scalar("DCF", "implied_tv_ebitda", "Implied terminal EV / NTM EBITDA", "{s:tv}/({last:IS.ebitda}*(1+{s:terminal_g}))", fmt="multiple"),
        Scalar("DCF", "implied_tv_pe", "Implied terminal P/E proxy (TV / NOPAT N+1)", "{s:tv}/{s:nopat_t1}", fmt="multiple"),
        Scalar("DCF", "dps_12m", "Expected dividends per share, next 12 months", f"{{f{k12}:IS.dps}}", fmt="per_share"),
        Scalar("DCF", "target_12m", "12-month price target (value x (1+ke) - dividends)",
               "{s:value_per_share}*(1+{s:ke})-{s:dps_12m}", fmt="per_share", style="total"),
        Scalar("DCF", "exp_total_return", "Expected 12m total return to target", "({s:target_12m}+{s:dps_12m})/{s:price}-1", fmt="percent"),
    )
    # ------------------------------------------------------------------ Sensitivity (WACC x g), native formulas
    step = 0.005
    for i in range(5):
        dw = (i - 2) * step
        for j in range(5):
            dg = (j - 2) * step
            W = f"({{s:wacc}}+({dw}))"
            G = f"({{s:terminal_g}}+({dg}))"
            pv = "+".join(f"{{f{k}:DCF.fcff_incl}}/(1+{W})^{{f{k}:DCF.time}}" for k in range(N))
            tv = f"{{last:DCF.nopat}}*(1+{G})*(1-{G}/{{s:ronic}})/({W}-{G})*(1+{W})^0.5/(1+{W})^{{s:time_tv}}"
            tmpl = f"(({pv})+{tv}+{{s:fin_assets_latest}}-{{s:debt_latest}}-{{s:nci_latest}}-{{s:lease_adj}})/{{s:diluted_shares}}"
            spec.scalar(Scalar("Sensitivity", f"sens_{i}_{j}", "", tmpl, fmt="per_share", cell=f"{chr(ord('D') + j)}{9 + i}"))
    # ------------------------------------------------------------------ Checks
    spec.scalar(
        Scalar("Checks", "chk_bs", "Forecast BS: max |net assets - capital|", "MAX(ABS(MIN({range:BS.check})),ABS(MAX({range:BS.check})))",
               fmt="millions_1dp", style="check"),
        Scalar("Checks", "chk_hist_bs", "Historical BS identity: max |A - L - E - NCI| (last year)",
               "ABS({hl:Hist_Data.bs_identity_diff})", fmt="millions_1dp", style="check"),
        Scalar("Checks", "chk_sens_center", "Sensitivity centre = DCF value per share (difference)", "{s:sens_2_2}-{s:value_per_share}",
               fmt="per_share", style="check"),
        Scalar("Checks", "chk_sens_wacc", "Value falls as WACC rises (low-WACC minus high-WACC value, must be > 0)",
               "{s:sens_0_2}-{s:sens_4_2}", fmt="per_share", style="check"),
        Scalar("Checks", "chk_sens_g", "Value rises with terminal growth when RONIC > WACC (must be >= 0)",
               "{s:sens_2_4}-{s:sens_2_0}", fmt="per_share", style="check"),
        Scalar("Checks", "chk_g_rf", "Terminal growth below risk-free rate (rf - g, must be >= 0)", "{s:rf}-{s:terminal_g}",
               fmt="percent", style="check"),
        Scalar("Checks", "chk_wacc_g", "WACC exceeds terminal growth (must be > 0)", "{s:wacc}-{s:terminal_g}", fmt="percent", style="check"),
        Scalar("Checks", "chk_bridge", "Per-share x shares = equity value (difference)", "{s:value_per_share}*{s:diluted_shares}-{s:equity_value}",
               fmt="millions_1dp", style="check"),
    )
    spec.layout()
    return spec


def _next12_index(fc_ends: list[dt.date], val_date: dt.date) -> int:
    """Forecast column whose fiscal year overlaps most with the next 12 months."""
    best, best_overlap = 0, -1
    horizon_end = val_date + dt.timedelta(days=365)
    for k, end in enumerate(fc_ends):
        start = end - dt.timedelta(days=365)
        overlap = (min(end, horizon_end) - max(start, val_date)).days
        if overlap > best_overlap:
            best, best_overlap = k, overlap
    return best
