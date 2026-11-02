"""Institutional report and one-to-two page summary (reportlab). Black, white
and muted gold; serif display type, sans body; every table states units and
source; tables are sized to the frame and wrap text (never clipped)."""
from __future__ import annotations

import html
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, Image, KeepTogether, NextPageTemplate, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

from ..util import fmt_pct
from .theme import c, pdf_fonts, tokens

W, H = A4
MARGIN = 18 * mm
FRAME_W = W - 2 * MARGIN
LAYOUT_LOG: list[dict] = []   # table-width audit used by the visual gate


def col(name):
    return colors.HexColor(c(name))


def styles():
    f = pdf_fonts()
    ts = tokens()["type_scale_pt"]
    base = dict(fontName=f["sans"], fontSize=ts["body"], leading=ts["body"] * 1.42, textColor=col("ink"))
    return {
        "body": ParagraphStyle("body", **base, spaceAfter=4),
        "small": ParagraphStyle("small", **{**base, "fontSize": ts["small"], "leading": ts["small"] * 1.35, "textColor": col("grey_600")}),
        "cell": ParagraphStyle("cell", **{**base, "fontSize": ts["table"], "leading": ts["table"] * 1.25}),
        "cellr": ParagraphStyle("cellr", **{**base, "fontSize": ts["table"], "leading": ts["table"] * 1.25, "alignment": TA_RIGHT}),
        "cellb": ParagraphStyle("cellb", **{**base, "fontName": f["sans_bold"], "fontSize": ts["table"], "leading": ts["table"] * 1.25}),
        "cellbr": ParagraphStyle("cellbr", **{**base, "fontName": f["sans_bold"], "fontSize": ts["table"], "leading": ts["table"] * 1.25,
                                              "alignment": TA_RIGHT}),
        "h1": ParagraphStyle("h1", fontName=f["serif_bold"], fontSize=ts["h1"], leading=ts["h1"] * 1.2, textColor=col("ink"),
                             spaceBefore=6, spaceAfter=3),
        "h2": ParagraphStyle("h2", fontName=f["sans_bold"], fontSize=ts["h2"] - 1, leading=ts["h2"] * 1.25, textColor=col("gold_dark"),
                             spaceBefore=8, spaceAfter=3),
        "bullet": ParagraphStyle("bullet", **base, leftIndent=10, bulletIndent=0, spaceAfter=2),
        "cover_title": ParagraphStyle("ct", fontName=f["serif_bold"], fontSize=ts["cover"], leading=ts["cover"] * 1.15,
                                      textColor=colors.white),
        "cover_sub": ParagraphStyle("cs", fontName=f["sans"], fontSize=10, leading=13, textColor=col("gold_light")),
        "kpi_label": ParagraphStyle("kl", fontName=f["sans"], fontSize=7, leading=9, textColor=col("grey_600")),
        "kpi_value": ParagraphStyle("kv", fontName=f["serif_bold"], fontSize=12, leading=14.5, textColor=col("ink")),
        "caption": ParagraphStyle("cap", fontName=f["sans_italic"], fontSize=7, leading=9, textColor=col("grey_600"), spaceAfter=6),
    }


def esc(s) -> str:
    t = html.escape(str(s if s is not None else ""), quote=False)
    n = len(t) - len(t.lstrip(" "))
    return "&nbsp;&nbsp;" * n + t.lstrip(" ") if n else t


def P(text, st):
    return Paragraph(text, st)


def money(v, dp=2):
    if v is None:
        return "n/a"
    if abs(v) < 0.5 * 10 ** (-dp):
        v = 0.0
    return f"({abs(v):,.{dp}f})" if v < 0 else f"{v:,.{dp}f}"


def mn(v):
    return "n/a" if v is None else money(v / 1e6, 0)


def table(rows, widths_mm, S, header=True, numeric_cols=(), zebra=True, bold_last=False, name="table"):
    widths = [w * mm for w in widths_mm]
    total = sum(widths)
    LAYOUT_LOG.append({"table": name, "width_pt": round(total, 1), "frame_pt": round(FRAME_W, 1), "fits": total <= FRAME_W + 0.5})
    if total > FRAME_W:  # scale down rather than clip
        widths = [w * FRAME_W / total for w in widths]
    data = []
    for i, r in enumerate(rows):
        out = []
        for j, v in enumerate(r):
            if isinstance(v, Paragraph):
                out.append(v)
            else:
                if header and i == 0:
                    stl = S["cellbr"] if j in numeric_cols else S["cellb"]
                else:
                    stl = S["cellr"] if j in numeric_cols else S["cell"]
                out.append(P(esc(v), stl))
        data.append(out)
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style += [("LINEBELOW", (0, 0), (-1, 0), 0.8, col("ink")), ("LINEABOVE", (0, 0), (-1, 0), 1.4, col("gold"))]
    style += [("LINEBELOW", (0, -1), (-1, -1), 0.5, col("grey_300"))]
    if zebra:
        for i in range(1 if header else 0, len(data)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), col("grey_100")))
    if bold_last:
        style.append(("LINEABOVE", (0, -1), (-1, -1), 0.6, col("ink")))
    t.setStyle(TableStyle(style))
    return t


def bullets(items, S):
    return [Paragraph(esc(x), S["bullet"], bulletText="\u2022") for x in items if x]


class _Doc(BaseDocTemplate):
    def __init__(self, path, D, **kw):
        super().__init__(str(path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN + 6 * mm,
                         bottomMargin=MARGIN, title=kw.pop("title", ""), author="eqr research platform", **kw)
        self.D = D
        body = Frame(MARGIN, MARGIN, FRAME_W, H - 2 * MARGIN - 6 * mm, id="body")
        cover = Frame(MARGIN, MARGIN, FRAME_W, H - MARGIN - 70 * mm, id="cover")
        self.addPageTemplates([PageTemplate("cover", [cover], onPage=self._cover), PageTemplate("body", [body], onPage=self._page)])

    def _watermark(self, cv):
        if self.D["synthetic"]:
            f = pdf_fonts()
            cv.saveState()
            cv.setFont(f["sans_bold"], 40)
            cv.setFillColor(col("negative"))
            cv.setFillAlpha(0.10)
            cv.translate(W / 2, H / 2)
            cv.rotate(35)
            cv.drawCentredString(0, 0, "SYNTHETIC TEST DATA")
            cv.restoreState()

    def _cover(self, cv, doc):
        f = pdf_fonts()
        cv.saveState()
        cv.setFillColor(col("ink"))
        cv.rect(0, H - 62 * mm, W, 62 * mm, stroke=0, fill=1)
        cv.setFillColor(col("gold"))
        cv.rect(0, H - 63.4 * mm, W, 1.4 * mm, stroke=0, fill=1)
        cv.setFillColor(col("gold_light"))
        cv.setFont(f["sans"], 8)
        cv.drawString(MARGIN, H - 12 * mm, "EQUITY RESEARCH  |  " + self.D["report_kind"].upper())
        cv.drawRightString(W - MARGIN, H - 12 * mm, f"As of {self.D['as_of']}")
        cv.setFillColor(colors.white)
        cv.setFont(f["serif_bold"], 24)
        cv.drawString(MARGIN, H - 30 * mm, self.D["security"]["name"][:48])
        cv.setFont(f["sans"], 10)
        cv.setFillColor(col("gold_light"))
        s = self.D["security"]
        cv.drawString(MARGIN, H - 39 * mm, f"{s['ticker']}  |  {s.get('exchange') or ''}  |  {s.get('identifier', '')}  |  "
                                           f"{(s.get('classification') or '').title()}")
        cv.setFillColor(colors.white)
        cv.setFont(f["sans_bold"], 12)
        cv.drawString(MARGIN, H - 52 * mm, f"{self.D['rating']['rating']}   |   Fair value {money(self.D['rec'].get('fair_value'), 0)} "
                                           f"{self.D['currency']}   |   Price {money(self.D['price'])}")
        self._footer(cv, doc)
        self._watermark(cv)
        cv.restoreState()

    def _page(self, cv, doc):
        f = pdf_fonts()
        cv.saveState()
        cv.setStrokeColor(col("gold"))
        cv.setLineWidth(0.8)
        cv.line(MARGIN, H - MARGIN, W - MARGIN, H - MARGIN)
        cv.setFont(f["sans_bold"], 7.5)
        cv.setFillColor(col("ink"))
        s = self.D["security"]
        cv.drawString(MARGIN, H - MARGIN + 3, f"{s['name']} ({s['ticker']})")
        cv.setFont(f["sans"], 7.5)
        cv.setFillColor(col("grey_600"))
        cv.drawRightString(W - MARGIN, H - MARGIN + 3, f"{self.D['report_kind']}  |  As of {self.D['as_of']}  |  {self.D['label']}")
        self._footer(cv, doc)
        self._watermark(cv)
        cv.restoreState()

    def _footer(self, cv, doc):
        f = pdf_fonts()
        cv.setStrokeColor(col("grey_300"))
        cv.setLineWidth(0.4)
        cv.line(MARGIN, MARGIN - 4 * mm, W - MARGIN, MARGIN - 4 * mm)
        cv.setFont(f["sans"], 6.5)
        cv.setFillColor(col("grey_600"))
        cv.drawString(MARGIN, MARGIN - 8 * mm, "Generated by the eqr research platform. Not investment advice. Sources and methodology in appendix.")
        cv.drawRightString(W - MARGIN, MARGIN - 8 * mm, f"Page {doc.page}")


def kpi_strip(D, S):
    rec = D["rec"]
    cells = [("Rating", D["rating"]["rating"]), ("Fair value", money(rec.get("fair_value"), 0)),
             ("Range", f"{money(rec.get('range_low'), 0)}-{money(rec.get('range_high'), 0)}"),
             ("12m target", money(rec.get("target_12m"), 0)), ("Price", money(D["price"])),
             ("Exp. return", fmt_pct(rec.get("expected_return"))), ("Confidence", D["confidence"])]
    row = [[P(esc(k), S["kpi_label"]) for k, _ in cells], [P(esc(v), S["kpi_value"]) for _, v in cells]]
    wts = [1.35, 1, 1.15, 1, 1, 1, 1]
    t = Table(row, colWidths=[FRAME_W * w / sum(wts) for w in wts])
    t.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 1.4, col("gold")), ("LINEBELOW", (0, 1), (-1, 1), 0.6, col("ink")),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    LAYOUT_LOG.append({"table": "kpi_strip", "width_pt": round(FRAME_W, 1), "frame_pt": round(FRAME_W, 1), "fits": True})
    return t


def _img(path, width_mm):
    from PIL import Image as PILImage
    with PILImage.open(path) as im:
        w, h = im.size
    iw = width_mm * mm
    return Image(str(path), width=iw, height=iw * h / w)


# =============================================================================== report
def build_report(path: Path, D: dict) -> None:
    S = styles()
    syn = D["synth"]
    story = [NextPageTemplate("body")]
    cur = D["currency"]
    if D["synthetic"]:
        story.append(P("<b>SYNTHETIC TEST DATA.</b> This document was produced from a fictional fixture to test the platform. "
                       "It describes no real company and must not be used for any investment decision.", S["body"]))
    story += [kpi_strip(D, S), Spacer(1, 5)]
    story.append(P(f"<b>{esc(syn['headline'])}</b>", S["body"]))
    story.append(P("Security identification", S["h2"]))
    s = D["security"]
    story.append(table([["Item", "Detail"],
                        ["Issuer", s["name"]], ["Ticker / exchange", f"{s['ticker']} / {s.get('exchange') or 'n/a'}"],
                        ["Identifier / classification", f"{s.get('identifier', '')} / {s.get('classification') or ''}"],
                        ["Reference price", f"{money(D['price'])} {cur} at {D['price_time']} - {D['price_source']}"],
                        ["Information cutoff", D["cutoff"]],
                        ["Latest financial period", D["latest_period_note"]],
                        ["Reporting currency / basis", f"{cur}; {D['taxonomy']}"],
                        ["Investment horizon", "12-24 months view; explicit forecast " f"{D['horizon']} years + terminal value"],
                        ["Document status", D["label"]]], [45, 129], S, name="security_id"))
    story.append(P("Executive summary", S["h1"]))
    story += bullets(syn["executive_summary"], S)
    story.append(CondPageBreak(75 * mm))
    story.append(_img(D["charts"]["football"], 168))
    story.append(P(f"Source: platform valuation engine; price {D['price_source']}. Bars show the bear-to-bull range for DCF and "
                   f"the full WACC x growth sensitivity range; comps bar shows peer interquartile-implied values where available.", S["caption"]))
    story.append(CondPageBreak(120 * mm))

    # 1 thesis
    story.append(P("1. Investment view, thesis and variant perception", S["h1"]))
    story.append(P(f"<b>View:</b> {esc(syn['view'])}. <b>Rating:</b> {esc(D['rating']['rating'])} - {esc(D['rating']['why'])}.", S["body"]))
    if syn["thesis_pillars"]:
        story.append(table([["Thesis pillar", "Argument", "Evidence"]] +
                           [[p["title"], p["argument"], ", ".join(p["evidence_ids"])] for p in syn["thesis_pillars"]],
                           [38, 116, 20], S, name="thesis"))
    story.append(P("What appears priced in", S["h2"]))
    story.append(P(esc(syn["priced_in"]), S["body"]))
    story.append(P("Where our analysis differs (variant perception)", S["h2"]))
    story.append(P(esc(syn["variant_perception"]), S["body"]))
    story.append(P("What must happen for upside / downside", S["h2"]))
    story.append(P("<b>Upside requires:</b> " + esc(syn["upside_requirements"]), S["body"]))
    story.append(P("<b>Downside triggers:</b> " + esc(syn["downside_triggers"]), S["body"]))
    story.append(P("What would falsify the thesis", S["h2"]))
    story += bullets(syn["falsification"] or ["Not specified (see evidence gaps)."], S)
    story.append(P("Assumptions that dominate valuation uncertainty", S["h2"]))
    story += bullets(syn["dominant_assumptions"], S)

    # 2 business & industry
    story.append(CondPageBreak(60 * mm))
    story.append(P("2. Business and industry analysis", S["h1"]))
    story.append(P("Business", S["h2"]))
    story.append(P(esc(syn["business_analysis"]), S["body"]))
    story.append(P("Industry and competitive position", S["h2"]))
    story.append(P(esc(syn["industry_analysis"]), S["body"]))

    # 3 historical
    story.append(CondPageBreak(110 * mm))
    story.append(P("3. Historical financial assessment", S["h1"]))
    h = D["hist_table"]
    story.append(table(h["rows"], [50] + [124 / (len(h["rows"][0]) - 1)] * (len(h["rows"][0]) - 1), S,
                       numeric_cols=tuple(range(1, len(h["rows"][0]))), name="hist"))
    story.append(P(f"{cur} millions except per-share and ratios. Source: {esc(s.get('fin_source', ''))} [{D['fin_ev']}]; value-level "
                   f"provenance for every value in the workbook 'Provenance' sheet.", S["caption"]))
    story.append(_img(D["charts"]["revenue"], 170))
    story.append(P(f"Source: {esc(s.get('fin_source', ''))} (history); platform forecast (gold).", S["caption"]))
    story.append(_img(D["charts"]["cash"], 170))
    story.append(P(f"Source: {esc(s.get('fin_source', ''))} - cash-flow statements.", S["caption"]))
    story.append(P(esc(syn["financial_assessment"]), S["body"]))
    if D["quality_flags"]:
        story.append(P("Financial-quality flags (computed)", S["h2"]))
        story += bullets(D["quality_flags"], S)
    story.append(P("Reconciliation", S["h2"]))
    story.append(P(esc(D["recon_summary"]), S["body"]))

    # 4 forecast
    story.append(CondPageBreak(110 * mm))
    story.append(P("4. Forecast rationale", S["h1"]))
    story.append(P(esc(syn["forecast_rationale"]), S["body"]))
    story.append(table([["Driver (base)", "Yr 1", f"Yr {D['horizon']}", "Rationale (analyst assumption)"]] +
                       [[a["label"], a["first"], a["last"], a["rationale"]] for a in D["assumption_rows"]],
                       [36, 14, 14, 110], S, numeric_cols=(1, 2), name="assumptions"))
    story.append(P("All forecast values are analyst assumptions generated by versioned rules (methodology/forecasting.yaml); "
                   "historical inputs are reported values.", S["caption"]))
    story.append(table(D["fc_table"]["rows"], [42] + [132 / (len(D["fc_table"]["rows"][0]) - 1)] * (len(D["fc_table"]["rows"][0]) - 1),
                       S, numeric_cols=tuple(range(1, len(D["fc_table"]["rows"][0]))), name="forecast"))
    story.append(P(f"{cur} millions except per-share. Base scenario. Source: workbook IS/CF/DCF sheets.", S["caption"]))

    # 5 valuation
    story.append(CondPageBreak(110 * mm))
    story.append(P("5. Valuation analysis and reconciliation", S["h1"]))
    story.append(P("Method selection", S["h2"]))
    story.append(table([["Method", "Status", "Weight", "Value/share", "Reason"]] +
                       [[m["label"], m["status"].upper(), fmt_pct(m["weight"], 0) if m["weight"] else "-",
                         money(D["values"].get(m["id"])) if D["values"].get(m["id"]) else "-", m["reason"]] for m in D["methods"]],
                       [40, 17, 13, 19, 85], S, numeric_cols=(2, 3), name="methods"))
    story.append(P("Cost of capital", S["h2"]))
    story.append(table([["Input", "Value", "Source / basis"]] + D["wacc_rows"], [45, 20, 109], S, numeric_cols=(1,), name="wacc"))
    story.append(P("DCF summary and enterprise-to-equity bridge (base case)", S["h2"]))
    story.append(table([["Item", f"{cur} m / per share"]] + D["dcf_rows"], [110, 64], S, numeric_cols=(1,), name="dcf"))
    story.append(_img(D["charts"]["bridge"], 165))
    story.append(P(f"Terminal value: {esc(D['tv_note'])}", S["body"]))
    story.append(KeepTogether([P("Sensitivity of value per share to WACC and terminal growth", S["h2"]),
                               table(D["sens_rows"], [30] + [28.8] * 5, S, numeric_cols=(1, 2, 3, 4, 5), name="sensitivity"),
                               P("Native-formula grid in the workbook 'Sensitivity' sheet; centre equals the base DCF value.", S["caption"])]))
    story.append(P("Reverse DCF: what the price requires", S["h2"]))
    story.append(P(esc(D["reverse"]["narrative_growth"]) + " " + esc(D["reverse"]["narrative_margin"]), S["body"]))
    if D.get("comps_rows"):
        story.append(P("Trading comparables", S["h2"]))
        story.append(table(D["comps_rows"], [20, 24, 26, 22, 22, 20, 40], S, numeric_cols=(1, 2, 3, 4, 5), name="comps"))
        story.append(P(esc(D["comps_note"]), S["caption"]))
    story.append(P("Reconciliation", S["h2"]))
    story.append(P(esc(D["recon_text"]), S["body"]))

    # 6 scenarios
    story.append(CondPageBreak(70 * mm))
    story.append(P("6. Bull, base, bear and stress cases", S["h1"]))
    story.append(table([["Scenario", "Definition (vs base)", "Value/share", "vs price", "WACC", "g", "Prob."]] + D["scen_rows"],
                       [18, 70, 20, 16, 14, 14, 22], S, numeric_cols=(2, 3, 4, 5, 6), name="scenarios"))
    story.append(P(f"Probability-weighted value (bear/base/bull): {money(D['pw_value'])} {cur}. Stress case is a tail test, not "
                   f"probability-weighted.", S["body"]))

    # 7 catalysts
    story.append(P("7. Catalysts", S["h1"]))
    if syn["catalysts"]:
        story.append(table([["Catalyst", "Timing", "Dependency", "Dated by evidence"]] +
                           [[x["event"], x["timing"], x["dependency"], ("yes " + ", ".join(x["evidence_ids"])) if x["dated_by_evidence"]
                             else "no (estimated)"] for x in syn["catalysts"]], [52, 30, 62, 30], S, name="catalysts"))
    else:
        story.append(P("No catalysts verified from evidence.", S["body"]))

    # 8 risks
    story.append(CondPageBreak(60 * mm))
    story.append(P("8. Risks, thesis-breakers and monitoring", S["h1"]))
    story.append(table([["Risk", "Transmission to value", "Thesis-breaker", "Monitor"]] +
                       [[r["risk"], r["transmission"], "YES" if r["thesis_breaker"] else "no", r["monitoring"]] for r in syn["risks"]],
                       [36, 78, 18, 42], S, name="risks"))
    story.append(P("Impact matrix", S["h2"]))
    story.append(table([["Factor", "Materiality", "Evidence", "Prob.", "Timeframe", "Model variables", "Evidence ids"]] +
                       [[m["factor"], m["materiality"], m["evidence_strength"], m["probability"], m["timeframe"],
                         ", ".join(m["model_variables"]), ", ".join(m["evidence_ids"]) or "-"] for m in syn["impact_matrix"]],
                       [40, 18, 22, 18, 20, 32, 24], S, name="impact"))
    story.append(P("Transmission mechanism for each factor is stated in the risk table: exposure -> operational effect -> line item -> value.",
                   S["caption"]))

    # 9 counter thesis
    story.append(P("9. Counter-thesis", S["h1"]))
    story.append(P("<b>The strongest case against our view:</b> " + esc(syn["counter_thesis"]["argument"]), S["body"]))
    story.append(P("<b>Evaluation:</b> " + esc(syn["counter_thesis"]["evaluation"]), S["body"]))

    # 10 governance
    story.append(P("10. Governance and capital allocation", S["h1"]))
    story.append(P(esc(syn["governance_capital_allocation"]), S["body"]))
    story.append(table(D["capalloc_rows"], [50] + [124 / (len(D["capalloc_rows"][0]) - 1)] * (len(D["capalloc_rows"][0]) - 1), S,
                       numeric_cols=tuple(range(1, len(D["capalloc_rows"][0]))), name="capalloc"))
    story.append(P(f"{cur} millions. Source: {esc(D['security'].get('fin_source', ''))} [{D['fin_ev']}].", S["caption"]))

    # 11 evidence gaps
    story.append(CondPageBreak(110 * mm))
    story.append(P("11. Evidence gaps, confidence and quality gates", S["h1"]))
    story.append(P(f"<b>Confidence: {esc(D['confidence'])}.</b> {esc(syn['confidence_rationale'])}", S["body"]))
    story += bullets(syn["evidence_gaps"], S)
    story.append(P("Quality gates", S["h2"]))
    story.append(table([["Gate", "Severity", "Status", "Detail"]] +
                       [[f"{g['id']} {g['name']}", g["severity"], g["status"], g["detail"]] for g in D["gates"]],
                       [62, 16, 20, 76], S, name="gates"))
    story.append(P("Independent critique", S["h2"]))
    story.append(P(esc(D["critique_summary"]), S["body"]))

    # appendix
    story.append(CondPageBreak(90 * mm))
    story.append(P("Appendix A. Methodology", S["h1"]))
    story += bullets(D["methodology_notes"], S)
    story.append(P("Appendix B. Source notes", S["h1"]))
    story.append(table([["Id", "Source", "Tier", "Published / period", "Retrieved (UTC)"]] +
                       [[x["id"], f"{x['title']} - {x['publisher']}" + (f" <{x['url']}>" if x["url"] else ""), x["tier"],
                         x["published_at"] or x["period"] or "", (x["retrieved_at"] or "")[:19]] for x in D["sources"]],
                       [14, 104, 9, 22, 25], S, name="sources"))
    if D["discrepancies"]:
        story.append(P("Appendix C. Discrepancy log", S["h1"]))
        story.append(table([["Type", "Period", "Item", "Resolution"]] +
                           [[d.get("type"), d.get("period"), d.get("item") or "", d.get("resolution")] for d in D["discrepancies"]],
                           [22, 20, 28, 104], S, name="discrepancies"))
    doc = _Doc(path, {**D, "report_kind": "Initiation report"}, title=f"{D['security']['name']} - equity research report")
    doc.build(story)


# =============================================================================== summary
def build_summary(path: Path, D: dict) -> None:
    S = styles()
    syn = D["synth"]
    cur = D["currency"]
    story = [NextPageTemplate("body")]
    if D["synthetic"]:
        story.append(P("<b>SYNTHETIC TEST DATA - fictional company, for platform testing only.</b>", S["body"]))
    story += [kpi_strip(D, S), Spacer(1, 4), P(f"<b>{esc(syn['headline'])}</b>", S["body"])]
    story.append(P("Conclusion", S["h2"]))
    story += bullets(syn["executive_summary"][:4], S)
    story.append(P("Thesis", S["h2"]))
    story += bullets([f"{p['title']}: {p['argument']}" + ("" if "[EV-" in p["argument"] else f" [{', '.join(p['evidence_ids'])}]")
                      for p in syn["thesis_pillars"][:4]]
                     or ["No evidence-based thesis (see report)."], S)
    story.append(P("Valuation", S["h2"]))
    story.append(table([["Measure", "Value"],
                        ["DCF value per share (base)", f"{money(D['dcf']['value_per_share'])} {cur}"],
                        ["Bear / bull", f"{money(D['scen']['bear']['value_per_share'], 0)} / {money(D['scen']['bull']['value_per_share'], 0)}"],
                        ["WACC / terminal growth", f"{fmt_pct(D['dcf']['wacc'])} / {fmt_pct(D['dcf']['terminal_g'])}"],
                        ["Terminal value share of EV", fmt_pct(D["dcf"]["tv_share"])],
                        ["Price implies (reverse DCF)", D["reverse"]["narrative_growth"]]], [60, 114], S, name="summary_val"))
    story.append(P("Catalysts", S["h2"]))
    story += bullets([f"{x['event']} ({x['timing']})" for x in syn["catalysts"][:4]] or ["None verified from evidence."], S)
    story.append(P("Largest risks", S["h2"]))
    story += bullets([f"{r['risk']}: {r['transmission']}" for r in syn["risks"][:4]], S)
    story.append(P(f"Confidence {esc(D['confidence'])}. Status: {esc(D['label'])}. Sources and full methodology in the report; "
                   f"price {esc(D['price_source'])}.", S["small"]))
    doc = _Doc(path, {**D, "report_kind": "Investment summary"}, title=f"{D['security']['name']} - investment summary")
    doc.build(story)
