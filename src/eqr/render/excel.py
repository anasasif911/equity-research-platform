"""Excel workbook: native formulas generated from the shared ModelSpec, plus
supporting sheets (read-me, dashboard, sources, provenance, scenarios,
reverse DCF, valuation selection/reconciliation, comps, discrepancies).

Conventions: blue font = hard-coded input; black = formula; grey italic =
memo; gold rule under titles; no hidden sheets, no iterative calculation."""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from ..model.sheetmodel import FIRST_DATA_COL, FIRST_ROW, ModelSpec
from .theme import tokens, xl

FMT = {
    "millions": '#,##0;(#,##0);"–"', "millions_1dp": '#,##0.0;(#,##0.0);"–"', "per_share": '#,##0.00;(#,##0.00);"–"',
    "percent": '0.0%;(0.0%);"–"', "ratio": '0.00', "ratio4": '0.0000', "multiple": '0.0"x"', "date": 'dd-mmm-yyyy',
    "int": '0', "text": '@',
}
FONT = "Calibri"
THIN = Side(style="thin", color=xl("grey_300"))
GOLD = Side(style="medium", color=xl("gold"))
INK = Side(style="thin", color=xl("ink"))

SHEET_TITLES = {
    "Hist_Data": "Historical data (as reported in issuer filings)", "Assumptions": "Assumptions and operating drivers",
    "IS": "Income statement", "BS": "Balance sheet (invested-capital form)", "CF": "Cash-flow statement",
    "Schedules": "Working capital, capex/PP&E, debt, distributions and share count", "WACC": "Cost of capital",
    "DCF": "FCFF discounted cash flow", "Sensitivity": "Sensitivity: value per share (WACC x terminal growth)",
    "Checks": "Automated model checks",
}


def _font(bold=False, italic=False, color="ink", size=9):
    return Font(name=FONT, bold=bold, italic=italic, color=xl(color), size=size)


def _title(ws, title: str, subtitle: str, width_cols: int):
    ws["A1"] = title
    ws["A1"].font = Font(name=FONT, bold=True, size=14, color=xl("ink"))
    ws["A2"] = subtitle
    ws["A2"].font = _font(color="grey_600", size=9)
    for col in range(1, width_cols + 1):
        ws.cell(row=3, column=col).border = Border(bottom=GOLD)
    ws.sheet_view.showGridLines = False


def _print(ws, landscape=True, title_rows="1:6"):
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = title_rows
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.oddFooter.center.text = "&8Page &P of &N"
    ws.oddFooter.left.text = "&8&A"


def _style_cell(cell, style: str, fmt: str | None):
    if fmt:
        cell.number_format = FMT.get(fmt, fmt)
    if style == "input":
        cell.font = _font(color="input_blue")
    elif style == "total":
        cell.font = _font(bold=True)
        cell.border = Border(top=INK)
    elif style == "memo":
        cell.font = _font(italic=True, color="grey_600")
    elif style == "check":
        cell.font = _font(color="grey_600")
    else:
        cell.font = _font()


def write_spec_sheets(wb: Workbook, spec: ModelSpec, company: str, currency: str):
    H, N = spec.H, spec.N
    last_col = FIRST_DATA_COL + H + N - 1
    note_col = last_col + 1
    for sheet in spec.sheets:
        ws = wb.create_sheet(sheet)
        rows = [r for r in spec.rows if r.sheet == sheet]
        scal = [s for s in spec.scalars if s.sheet == sheet]
        _title(ws, SHEET_TITLES.get(sheet, sheet), f"{company} | {currency} millions unless stated | "
                                                  f"blue = input, black = formula", note_col if rows else 5)
        ws.column_dimensions["A"].width = 52
        ws.column_dimensions["B"].width = 9
        if rows:
            ws.cell(row=5, column=1, value="Line item").font = _font(bold=True)
            ws.cell(row=5, column=2, value="Unit").font = _font(bold=True)
            for c, lab in enumerate(spec.hist_labels + spec.fc_labels):
                cell = ws.cell(row=5, column=FIRST_DATA_COL + c, value=lab)
                cell.font = _font(bold=True)
                cell.alignment = Alignment(horizontal="right")
                cell.border = Border(bottom=INK)
                if c >= H:
                    cell.fill = PatternFill("solid", fgColor=xl("gold_light"))
                ws.column_dimensions[get_column_letter(FIRST_DATA_COL + c)].width = 11.5
            ws.cell(row=4, column=FIRST_DATA_COL, value="Historical (reported)").font = _font(italic=True, color="grey_600", size=8)
            ws.cell(row=4, column=FIRST_DATA_COL + H, value="Forecast (analyst assumptions)").font = _font(italic=True, color="gold_dark", size=8)
            ws.cell(row=5, column=note_col, value="Notes / source").font = _font(bold=True)
            ws.column_dimensions[get_column_letter(note_col)].width = 40
        for r in rows:
            rr = spec.row_pos[(sheet, r.key)]
            a = ws.cell(row=rr, column=1, value=r.label)
            ws.cell(row=rr, column=2, value=r.unit).font = _font(color="grey_600", size=8)
            if r.style == "header":
                a.font = _font(bold=True, color="gold_dark")
                if r.note:
                    n = ws.cell(row=rr, column=note_col, value=r.note)
                    n.font = _font(italic=True, color="grey_600", size=8)
                    n.alignment = Alignment(wrap_text=True, vertical="top")
                    ws.row_dimensions[rr].height = max(15, 11 * (len(r.note) // 52 + 1))
                continue
            a.font = _font(bold=r.style == "total", italic=r.style == "memo",
                           color="grey_600" if r.style in ("memo", "check") else "ink")
            for c in range(H + N):
                src = r.fc if c >= H else r.hist
                if src is None:
                    continue
                cell = ws.cell(row=rr, column=FIRST_DATA_COL + c)
                if isinstance(src, list):
                    v = src[c - H] if c >= H else src[c]
                    if v is None:
                        continue
                    cell.value = v
                    _style_cell(cell, "input" if r.style in ("input",) or (isinstance(src, list) and r.style == "normal") else
                                ("memo" if r.style == "memo" else r.style), r.fmt)
                    if r.style == "memo" and c < H:
                        cell.font = _font(italic=True, color="grey_600")
                else:
                    cell.value = spec.to_excel(src, sheet, c)
                    _style_cell(cell, "normal" if r.style == "input" else r.style, r.fmt)
            note = r.note or r.source
            if note:
                n = ws.cell(row=rr, column=note_col, value=note)
                n.font = _font(italic=True, color="grey_600", size=8)
                n.alignment = Alignment(wrap_text=True, vertical="top")
        # scalars
        if scal:
            if not rows:
                ws.column_dimensions["C"].width = 16
                ws.column_dimensions["D"].width = 90
                ws.cell(row=5, column=1, value="Item").font = _font(bold=True)
                ws.cell(row=5, column=3, value="Value").font = _font(bold=True)
                ws.cell(row=5, column=4, value="Source / note").font = _font(bold=True)
            for s in scal:
                sh, cellref = spec.scalar_pos[s.name]
                cell = ws[cellref]
                cell.value = spec.to_excel(s.value, sheet, None) if isinstance(s.value, str) else s.value
                _style_cell(cell, s.style if s.style != "normal" else "normal", s.fmt)
                if not s.cell:
                    row = cell.row
                    lab = ws.cell(row=row, column=1, value=s.label)
                    lab.font = _font(bold=s.style == "total")
                    if s.source or s.note:
                        d = ws.cell(row=row, column=4 if not rows else note_col, value=s.source or s.note)
                        d.font = _font(italic=True, color="grey_600", size=8)
        ws.freeze_panes = ws.cell(row=6, column=FIRST_DATA_COL) if rows else "A6"
        _print(ws)


def finish_sensitivity(ws, spec: ModelSpec):
    ws.column_dimensions["C"].width = 14
    for col in "DEFGH":
        ws.column_dimensions[col].width = 13
    ws["C7"] = "WACC ↓ / g →"
    ws["C7"].font = _font(bold=True, size=8)
    for j in range(5):
        cell = ws.cell(row=8, column=4 + j, value=spec.to_excel(f"{{s:terminal_g}}+({(j - 2) * 0.005})", "Sensitivity", None))
        cell.number_format = FMT["percent"]
        cell.font = _font(bold=True)
        cell.border = Border(bottom=INK)
    for i in range(5):
        cell = ws.cell(row=9 + i, column=3, value=spec.to_excel(f"{{s:wacc}}+({(i - 2) * 0.005})", "Sensitivity", None))
        cell.number_format = FMT["percent"]
        cell.font = _font(bold=True)
    ws["D9"].parent  # noqa: B018
    ws.cell(row=11, column=6).fill = PatternFill("solid", fgColor=xl("gold_light"))
    ws.cell(row=11, column=6).font = _font(bold=True)
    ws["A15"] = ("Each cell is a native formula re-discounting the DCF sheet's FCFF row and terminal value at the row WACC and "
                 "column terminal growth (RONIC held at the base estimate). Centre cell equals the DCF value per share (see Checks).")
    ws["A15"].font = _font(italic=True, color="grey_600", size=8)
    ws["A15"].alignment = Alignment(wrap_text=True)
    ws.merge_cells("A15:H17")


def finish_checks(ws, spec: ModelSpec):
    rules = {"chk_bs": "ABS({v})<0.5", "chk_hist_bs": "ABS({v})<1", "chk_sens_center": "ABS({v})<0.005",
             "chk_sens_wacc": "{v}>0", "chk_sens_g": "{v}>=0", "chk_g_rf": "{v}>=0", "chk_wacc_g": "{v}>0",
             "chk_bridge": "ABS({v})<0.01"}
    ws.cell(row=5, column=5, value="Status").font = _font(bold=True)
    ws.column_dimensions["E"].width = 10
    rows = []
    for name, rule in rules.items():
        _, ref = spec.scalar_pos[name]
        r = int(ref[1:])
        rows.append(r)
        cell = ws.cell(row=r, column=5, value=f'=IF({rule.format(v=ref)},"PASS","FAIL")')
        cell.font = _font(bold=True)
    last = max(rows) + 2
    ws.cell(row=last, column=1, value="Overall model integrity").font = _font(bold=True)
    ws.cell(row=last, column=5, value=f'=IF(COUNTIF(E{min(rows)}:E{max(rows)},"FAIL")=0,"PASS","FAIL")').font = _font(bold=True, color="gold_dark")
    return f"Checks!$E${last}"


def _table(ws, start_row: int, headers: list[str], rows: list[list], fmts: list[str | None] | None = None,
           widths: list[float] | None = None, wrap_cols: tuple = ()):
    for j, h in enumerate(headers):
        c = ws.cell(row=start_row, column=1 + j, value=h)
        c.font = _font(bold=True)
        c.border = Border(bottom=INK)
        c.alignment = Alignment(wrap_text=True, vertical="bottom")
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            c = ws.cell(row=start_row + 1 + i, column=1 + j, value=v)
            c.font = _font()
            if fmts and j < len(fmts) and fmts[j] and isinstance(v, (int, float)):
                c.number_format = FMT.get(fmts[j], fmts[j])
            if j in wrap_cols:
                c.alignment = Alignment(wrap_text=True, vertical="top")
            if i % 2 == 1:
                c.fill = PatternFill("solid", fgColor=xl("grey_100"))
    if widths:
        for j, w in enumerate(widths):
            ws.column_dimensions[get_column_letter(1 + j)].width = w
    return start_row + 1 + len(rows)


def build_workbook(path: Path, *, spec: ModelSpec, data: dict) -> dict:
    """data: assembled run outputs (see render.__init__). Returns cell map for validation."""
    wb = Workbook()
    wb.remove(wb.active)
    sec, cur = data["security"], data["currency"]
    company = f"{sec['name']} ({sec['ticker']})"

    # ---------------------------------------------------------------- Read Me
    ws = wb.create_sheet("ReadMe")
    _title(ws, f"{company} - financial and valuation model", f"As of {data['as_of']} | Status: {data['label']}", 4)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110
    lines = [
        ("Security", f"{sec['name']} | ticker {sec['ticker']} | {sec.get('exchange') or ''} | {sec.get('identifier', '')} | "
                     f"{sec.get('classification') or ''}"),
        ("Information cutoff", data["cutoff"]),
        ("Reference price", f"{data['price']:,.2f} {cur} at {data['price_time']} ({data['price_source']})"),
        ("Reporting currency / units", f"{cur}; financial lines in millions; per-share in {cur}; shares in millions"),
        ("Accounting basis", f"{data['taxonomy']}; source: {sec.get('fin_source', '')}; fiscal years end {data['fye_note']}"),
        ("Model status", data["label"]),
        ("Data label", "SYNTHETIC TEST DATA - NOT A REAL COMPANY" if data["synthetic"] else "Live public sources (see Sources)"),
        ("Conventions", "Blue font = hard-coded input (historical data or assumption). Black = formula. Grey italic = memo. "
                        "Gold-shaded headers = forecast periods. No hidden sheets, no macros, no iterative calculation."),
        ("Scenario switch", "Assumptions!scenario (1 Base, 2 Bull, 3 Bear, 4 Stress) drives all model sheets live. The Scenarios sheet "
                            "stores engine results for all four simultaneously."),
        ("Circularity", "None. Interest income is earned on opening cash; WACC uses current market-value weights."),
        ("SBC", "Stock-based compensation is a real expense in EBIT. Forecast CFO excludes the SBC add-back; share count falls only "
                "by net repurchases, so dilution is not double counted."),
        ("Leases", data["lease_note"]),
        ("Forecast balance sheet", "Invested-capital form: cash & investments + operating NWC + net PP&E + other net operating "
                                   "assets (held flat) = gross debt + equity. BS!check must be zero in every year."),
        ("Valuation timing", "Valuation date = information cutoff. Cash flows are counted only after the latest balance-sheet date "
                             "(fraction row on DCF), discounted at mid-period. Present intrinsic value and the 12-month target are "
                             "bridged on DCF (target = value x (1 + ke) - expected dividends)."),
        ("Verification", "This workbook was recalculated by LibreOffice (independent engine) and compared cell-by-cell with the "
                         "platform engine; see the validation report in the run folder."),
        ("Sheets", "Dashboard, Assumptions, IS, BS, CF, Schedules, WACC, DCF, Sensitivity, Scenarios, Reverse_DCF, Valuation, "
                   "Comps, Hist_Data, Normalization, Provenance, Sources, Discrepancies, Checks"),
    ]
    for i, (k, v) in enumerate(lines):
        a = ws.cell(row=5 + i, column=1, value=k)
        a.font = _font(bold=True)
        b = ws.cell(row=5 + i, column=2, value=v)
        b.font = _font(color="negative" if (k == "Data label" and data["synthetic"]) else "ink", bold=k == "Data label" and data["synthetic"])
        b.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[5 + i].height = 14 * max(1, len(str(v)) // 120 + 1)
    _print(ws, landscape=False, title_rows="1:3")

    write_spec_sheets(wb, spec, company, cur)
    finish_sensitivity(wb["Sensitivity"], spec)
    overall_ref = finish_checks(wb["Checks"], spec)

    def sref(name):
        sh, cell = spec.scalar_pos[name]
        col, row = cell[0], cell[1:]
        return f"{sh}!${col}${row}" if sh.isalnum() else f"'{sh}'!${col}${row}"

    # ---------------------------------------------------------------- Dashboard
    ws = wb.create_sheet("Dashboard", 1)
    _title(ws, f"{company} - valuation dashboard", f"As of {data['as_of']} | {cur} | Status: {data['label']}", 6)
    ws.column_dimensions["A"].width = 44
    for col, w in zip("BCDEF", (16, 16, 16, 16, 16)):
        ws.column_dimensions[col].width = w
    kv = [
        ("Reference price", f"={sref('price')}", "per_share"),
        ("Intrinsic value per share - DCF (live scenario)", f"={sref('value_per_share')}", "per_share"),
        ("Upside / (downside)", f"={sref('upside')}", "percent"),
        ("12-month price target - DCF (live scenario)", f"={sref('target_12m')}", "per_share"),
        ("Enterprise value (m)", f"={sref('ev')}", "millions"),
        ("Equity value (m)", f"={sref('equity_value')}", "millions"),
        ("WACC", f"={sref('wacc')}", "percent"),
        ("Cost of equity", f"={sref('ke')}", "percent"),
        ("Terminal growth", f"={sref('terminal_g')}", "percent"),
        ("Terminal value share of EV", f"={sref('tv_share')}", "percent"),
        ("Implied terminal EV / NTM EBITDA", f"={sref('implied_tv_ebitda')}", "multiple"),
        ("Model integrity checks", f"={overall_ref}", None),
    ]
    ws["A5"] = "Live model outputs (formulas)"
    ws["A5"].font = _font(bold=True, color="gold_dark")
    cellmap = {}
    for i, (k, f, fmt) in enumerate(kv):
        ws.cell(row=6 + i, column=1, value=k).font = _font()
        c = ws.cell(row=6 + i, column=2, value=f)
        c.font = _font(bold=True)
        c.alignment = Alignment(horizontal="right")
        if fmt:
            c.number_format = FMT[fmt]
        cellmap[k] = f"Dashboard!B{6 + i}"
    r0 = 6 + len(kv) + 1
    ws.cell(row=r0, column=1, value="Reconciled view (engine; methods and weights on 'Valuation')").font = _font(bold=True, color="gold_dark")
    rec = data["reconciled"]
    recon_rows = [("Fair value estimate (reliability-weighted)", rec.get("fair_value"), "per_share"),
                  ("Valuation range - low", rec.get("range_low"), "per_share"),
                  ("Valuation range - high", rec.get("range_high"), "per_share"),
                  ("12-month price target", rec.get("target_12m"), "per_share"),
                  ("Expected 12m total return", rec.get("expected_return"), "percent"),
                  ("Rating", data["rating"]["rating"], None), ("Confidence", data["confidence"], None)]
    for i, (k, v, fmt) in enumerate(recon_rows):
        ws.cell(row=r0 + 1 + i, column=1, value=k).font = _font()
        c = ws.cell(row=r0 + 1 + i, column=2, value=v)
        c.font = _font(bold=True)
        c.alignment = Alignment(horizontal="right")
        if fmt:
            c.number_format = FMT[fmt]
    r1 = r0 + len(recon_rows) + 2
    ws.cell(row=r1, column=1, value="Scenarios (engine results)").font = _font(bold=True, color="gold_dark")
    sc = data["scenarios"]
    _table(ws, r1 + 1, ["Scenario", "Value / share", "Upside", "WACC", "Terminal g", "Probability"],
           [[sc[k]["label"], sc[k]["value_per_share"], sc[k]["upside"], sc[k]["wacc"], sc[k]["terminal_g"], sc[k]["probability"]]
            for k in ("bear", "base", "bull", "stress")],
           [None, "per_share", "percent", "percent", "percent", "percent"])
    _print(ws, landscape=False, title_rows="1:3")

    # ---------------------------------------------------------------- Scenarios
    ws = wb.create_sheet("Scenarios")
    _title(ws, "Scenario analysis", "Engine results for all scenarios (the live model shows the scenario selected on Assumptions)", 8)
    hdr = ["Scenario", "Definition", "Value / share", "Upside", "EV (m)", "WACC", "Terminal g", "TV share", "Final-year revenue (m)",
           "Final-year EBIT margin", "Probability"]
    rows = [[sc[k]["label"], ", ".join(f"{a}={b}" for a, b in sc[k]["definition"].items()), sc[k]["value_per_share"],
             sc[k]["upside"], sc[k]["ev"], sc[k]["wacc"], sc[k]["terminal_g"], sc[k]["tv_share"], sc[k]["revenue_final"],
             sc[k]["ebit_margin_final"], sc[k]["probability"]] for k in ("bear", "base", "bull", "stress")]
    end = _table(ws, 5, hdr, rows, [None, None, "per_share", "percent", "millions", "percent", "percent", "percent", "millions",
                                    "percent", "percent"], [16, 48, 13, 10, 14, 9, 10, 9, 14, 12, 11], wrap_cols=(1,))
    ws.cell(row=end + 1, column=1, value="Probability-weighted value (bear/base/bull)").font = _font(bold=True)
    c = ws.cell(row=end + 1, column=3, value=sc["probability_weighted_value"])
    c.number_format = FMT["per_share"]
    ws.cell(row=end + 3, column=1, value="Margin x growth sensitivity (engine, full re-forecast; value per share)").font = _font(bold=True, color="gold_dark")
    mg = data["margin_growth_grid"]
    ws.cell(row=end + 4, column=1, value="EBIT margin shift ↓ / growth shift →").font = _font(size=8, italic=True)
    for j, g in enumerate(mg["growth_shifts"]):
        cc = ws.cell(row=end + 4, column=3 + j, value=g)
        cc.number_format = '+0.0%;-0.0%;0.0%'
        cc.font = _font(bold=True)
    for i, m in enumerate(mg["margin_shifts"]):
        cc = ws.cell(row=end + 5 + i, column=2, value=m)
        cc.number_format = '+0.0%;-0.0%;0.0%'
        cc.font = _font(bold=True)
        for j, v in enumerate(mg["values"][i]):
            x = ws.cell(row=end + 5 + i, column=3 + j, value=v)
            x.number_format = FMT["per_share"]
    _print(ws)

    # ---------------------------------------------------------------- Reverse DCF
    ws = wb.create_sheet("Reverse_DCF")
    _title(ws, "Reverse DCF - what the price implies", "Solved by the engine (bisection) on the same model; enter the implied "
                                                      "shift on Assumptions to reproduce", 4)
    rd = data["reverse_dcf"]
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 16
    items = [("Reference price", rd["price"], "per_share"),
             ("Uniform revenue-growth shift that equates value to price", rd["growth_shift"], "percent"),
             ("Implied revenue CAGR over the horizon", rd.get("implied_revenue_cagr"), "percent"),
             ("Base-case revenue CAGR", rd["model_revenue_cagr"], "percent"),
             ("Uniform EBIT-margin shift that equates value to price", rd["margin_shift"], "percent"),
             ("Implied average EBIT margin", rd.get("implied_avg_margin"), "percent"),
             ("Base-case average EBIT margin", rd["model_avg_margin"], "percent")]
    for i, (k, v, f) in enumerate(items):
        ws.cell(row=5 + i, column=1, value=k).font = _font()
        c = ws.cell(row=5 + i, column=2, value=v if v is not None else "not attainable")
        c.font = _font(bold=True)
        c.number_format = FMT[f]
    ws.cell(row=13, column=1, value=rd["narrative_growth"]).alignment = Alignment(wrap_text=True)
    ws.cell(row=14, column=1, value=rd["narrative_margin"]).alignment = Alignment(wrap_text=True)
    ws.row_dimensions[13].height = 30
    ws.row_dimensions[14].height = 30
    _print(ws, landscape=False)

    # ---------------------------------------------------------------- Valuation (selection + reconciliation)
    ws = wb.create_sheet("Valuation")
    _title(ws, "Valuation method selection and reconciliation", "methodology/valuation_selection.yaml", 5)
    end = _table(ws, 5, ["Method", "Status", "Weight", "Value / share", "Reason"],
                 [[m["label"], m["status"].upper(), m["weight"], data["values"].get(m["id"]), m["reason"]] for m in data["methods"]],
                 [None, None, "percent", "per_share", None], [38, 11, 9, 13, 100], wrap_cols=(4,))
    ws.cell(row=end + 1, column=1, value="Methods are not mechanically averaged: weights reflect relative reliability for this "
                                         "archetype and are renormalised over methods that produced a defensible value.").font = _font(italic=True, size=8, color="grey_600")
    r = end + 3
    for k, v, f in recon_rows[:5]:
        ws.cell(row=r, column=1, value=k).font = _font()
        c = ws.cell(row=r, column=4, value=v)
        c.number_format = FMT[f]
        c.font = _font(bold=True)
        r += 1
    _print(ws)

    # ---------------------------------------------------------------- Comps
    ws = wb.create_sheet("Comps")
    _title(ws, "Trading comparables", "Peer market data with timestamps; multiples are formulas", 10)
    cs = data.get("comps")
    if cs and cs.get("peers"):
        hdr = ["Ticker", "Name", "Price", "Price time", "Shares (m)", "Market cap (m)", "Cash & inv. (m)", "Debt (m)", "EV (m)",
               "Revenue (m)", "EBITDA (m)", "Net income (m)", "EV/Sales", "EV/EBITDA", "P/E", "Basis", "Evidence"]
        for j, h in enumerate(hdr):
            c = ws.cell(row=5, column=1 + j, value=h)
            c.font = _font(bold=True)
            c.border = Border(bottom=INK)
            c.alignment = Alignment(wrap_text=True)
        for i, p in enumerate(cs["peers"]):
            r = 6 + i
            vals = [p["ticker"], p["name"], p["price"], p["price_time"], p["shares"] / 1e6, None, p["fin_assets"] / 1e6,
                    p["debt"] / 1e6, None, (p["ttm_revenue"] or 0) / 1e6, (p["ttm_ebitda"] or 0) / 1e6,
                    (p["ttm_net_income"] or 0) / 1e6, None, None, None, p["ttm_basis"], ", ".join(p["evidence_ids"])]
            for j, v in enumerate(vals):
                ws.cell(row=r, column=1 + j, value=v).font = _font(color="input_blue" if isinstance(v, (int, float)) else "ink")
            ws.cell(row=r, column=6, value=f"=C{r}*E{r}")
            ws.cell(row=r, column=9, value=f"=F{r}+H{r}-G{r}")
            ws.cell(row=r, column=13, value=f"=IF(J{r}>0,I{r}/J{r},\"n/m\")")
            ws.cell(row=r, column=14, value=f"=IF(K{r}>0,I{r}/K{r},\"n/m\")")
            ws.cell(row=r, column=15, value=f"=IF(L{r}>0,F{r}/L{r},\"n/m\")")
            for j, f in zip(range(3, 16), ["per_share", None, "millions_1dp", "millions", "millions", "millions", "millions",
                                           "millions", "millions", "millions", "multiple", "multiple", "multiple"]):
                if f:
                    ws.cell(row=r, column=j).number_format = FMT[f]
        last = 5 + len(cs["peers"])
        ws.cell(row=last + 1, column=12, value="Median").font = _font(bold=True)
        for col in "MNO":
            c = ws[f"{col}{last + 1}"]
            c.value = f'=IF(COUNT({col}6:{col}{last})>0,MEDIAN({col}6:{col}{last}),"n/m")'
            c.number_format = FMT["multiple"]
            c.font = _font(bold=True)
        for j, w in enumerate([8, 26, 10, 22, 10, 12, 12, 11, 12, 12, 12, 12, 9, 10, 8, 14, 20]):
            ws.column_dimensions[get_column_letter(1 + j)].width = w
        ws.cell(row=last + 3, column=1, value="Implied value per share (engine): " + ", ".join(
            f"{k} {v:,.2f}" for k, v in cs["implied"].items())).font = _font()
    else:
        ws["A5"] = ("Trading comparables not used: " + next((m["reason"] for m in data["methods"] if m["id"] == "trading_comps"), "n/a"))
        ws["A5"].font = _font(italic=True)
    _print(ws)

    # ---------------------------------------------------------------- Normalization
    ws = wb.create_sheet("Normalization")
    _title(ws, "Historical normalization bridge", "Reported EBIT to normalized EBIT using tagged non-recurring items only", 5)
    rows = []
    for b in data["bridge"]:
        adj = "; ".join(f"{a['label']} {a['amount'] / 1e6:,.0f}m ({a['concept']}, {a['accn']})" for a in b["adjustments"]) or "none tagged"
        rows.append([b["period"], (b["reported_ebit"] or 0) / 1e6, sum(a["amount"] for a in b["adjustments"]) / 1e6,
                     (b["normalized_ebit"] or 0) / 1e6, adj])
    _table(ws, 5, ["Period", "Reported EBIT (m)", "Adjustments (m)", "Normalized EBIT (m)", "Adjustment detail"], rows,
           [None, "millions", "millions", "millions", None], [10, 16, 16, 18, 100], wrap_cols=(4,))
    ws.cell(row=7 + len(rows), column=1, value=data["sbc_policy"]).font = _font(italic=True, size=8, color="grey_600")
    _print(ws)

    # ---------------------------------------------------------------- Provenance
    ws = wb.create_sheet("Provenance")
    _title(ws, "Cell-level provenance of historical inputs", "Every Hist_Data value -> source label/concept, document and page (or accession), filing date", 8)
    _table(ws, 5, ["Line item", "Period end", "Value", "Source label / concept", "Document ref", "Document", "Filed", "Evidence id"],
           data["provenance_rows"], [None, None, "millions", None, None, None, None, None], [28, 12, 16, 52, 24, 8, 12, 12])
    ws.freeze_panes = "A6"
    _print(ws)

    # ---------------------------------------------------------------- Sources
    ws = wb.create_sheet("Sources")
    _title(ws, "Research source register", "Tier 1 = primary regulatory/company ... tier 4 = aggregator. Retrieved documents are untrusted data.", 9)
    _table(ws, 5, ["Id", "Title", "Publisher", "Tier", "Period", "Published", "Retrieved (UTC)", "Method", "URL", "SHA-256 (12)"],
           [[s["id"], s["title"], s["publisher"], s["tier"], s["period"], s["published_at"], s["retrieved_at"], s["method"],
             s["url"], (s["sha256"] or "")[:12]] for s in data["sources"]],
           None, [9, 48, 26, 5, 12, 12, 22, 12, 60, 14], wrap_cols=(1, 8))
    ws.freeze_panes = "A6"
    _print(ws)

    # ---------------------------------------------------------------- Discrepancies
    ws = wb.create_sheet("Discrepancies")
    _title(ws, "Discrepancy log and reconciliation results", "Restatements, failed identities and their resolution", 6)
    end = _table(ws, 5, ["Type", "Period", "Item", "Difference", "Resolution"],
                 [[d.get("type"), d.get("period"), d.get("item"), d.get("diff"), d.get("resolution")] for d in data["discrepancies"]]
                 or [["none", "", "", None, "No discrepancies recorded"]], [None, None, None, "millions", None],
                 [14, 12, 22, 16, 100], wrap_cols=(4,))
    _table(ws, end + 2, ["Check", "Period", "Pass", "Difference", "Detail"],
           [[c["check"], c["period"], {True: "PASS", False: "FAIL", None: "N/A"}[c["pass"]], c.get("diff"), c.get("detail", "")]
            for c in data["recon_checks"]], [None, None, None, "millions", None])
    _print(ws)

    order = ["ReadMe", "Dashboard", "Assumptions", "IS", "BS", "CF", "Schedules", "WACC", "DCF", "Sensitivity", "Scenarios",
             "Reverse_DCF", "Valuation", "Comps", "Hist_Data", "Normalization", "Provenance", "Sources", "Discrepancies", "Checks"]
    wb._sheets = [wb[n] for n in order]
    for ws in wb.worksheets:
        ws.sheet_properties.tabColor = xl("gold") if ws.title in ("Dashboard", "DCF") else xl("ink")
    wb.calculation.fullCalcOnLoad = True
    wb.save(path)
    return {"dashboard": cellmap, "overall_check": overall_ref}
