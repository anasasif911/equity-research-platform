"""PSX market support: OCR-tolerant amounts, label normalisation, conflict
resolution by accounting identity, PSX page / GIS / Damodaran parsers.
Uses small inline snippets shaped like the real pages (no network)."""
import pytest

from eqr.extract import pdf_tables as T
from eqr.extract import psx_financials as X
from eqr.sources import damodaran, psx


@pytest.mark.parametrize("tok,ps,val", [
    ("81,654.298,337", False, 81654298337.0),       # OCR read a comma as a period
    ("1.000.000,000", False, 1000000000.0),
    ("(145,239,711,762)", False, -145239711762.0),
    ("-", False, 0.0), (".", False, 0.0), ("■", False, 0.0),
    ("390.51", True, 390.51), ("23J604J642JJ51", False, None),
])
def test_parse_amount_ocr_tolerant(tok, ps, val):
    assert T.parse_amount(tok, per_share=ps) == val


def test_norm_label_strips_merged_section_headings():
    assert T.norm_label("Current Liabilities Trade and other payables") == "trade and other payables"
    assert T.norm_label("Current Assets Stores, spares and loose tools") == "stores, spares and loose tools"


def _c(v, src, pr, page=1):
    return {"value": v, "source": src, "priority": pr, "page": page, "raw_label": "x", "raw_token": str(v),
            "evidence_id": "EV-T", "filed": "2026-09-04", "doc": "AR"}


def test_conflict_resolved_by_identity_not_priority():
    # audited page OCR-misread finance cost by 100; six-year summary satisfies the identity
    cands = {("ebit_before_finance", 2026): [_c(39_099_757_824, "six-year", 2)],
             ("interest_expense", 2026): [_c(-454_205_548, "audited", 1), _c(-454_205_648, "six-year", 2)],
             ("pretax_before_levy", 2026): [_c(38_645_552_176, "audited", 1)]}
    disc = []
    ch = X.resolve(cands, [2026], disc)
    assert ch[2026]["interest_expense"]["value"] == -454_205_648
    assert disc and disc[0]["type"] == "source_conflict" and "identit" in disc[0]["resolution"]


def test_levy_presentation_difference_is_not_an_error():
    cands = {("pretax_before_levy", 2025): [_c(26_739_280_758, "audited", 1)],
             ("levy", 2025): [_c(-1_431_417, "audited", 1)],
             ("pretax_income", 2025): [_c(26_737_849_341, "audited", 1), _c(26_739_280_758, "six-year", 2)]}
    disc = []
    ch = X.resolve(cands, [2025], disc)
    assert ch[2025]["pretax_income"]["value"] == 26_737_849_341
    assert disc and disc[0]["type"] == "definition"


def test_canonical_signs_and_tax_including_levy():
    ch = {2025: {k: _c(v, "audited", 1) for k, v in {
        "revenue": 100.0, "cogs": -70.0, "gross_profit": 30.0, "distribution": -5.0, "admin": -2.0, "other_opex": -1.0,
        "operating_income": 22.0, "other_income": 3.0, "interest_expense": -1.0, "pretax_before_levy": 24.0, "levy": -0.5,
        "pretax_income": 23.5, "income_tax": -9.0, "net_income": 14.5, "eps_diluted": 1.45, "capex": -4.0}.items()}}
    a = X.to_canonical(ch, 6, "PKR")
    end = "2025-06-30"
    v = {k: d[end] for k, d in a["values"].items()}
    assert v["cogs"] == 70 and v["sga"] == 7 and v["interest_expense"] == 1 and v["capex"] == -4
    assert v["pretax_income"] == 24 and v["income_tax"] == 9.5 and v["net_income"] == 14.5
    assert v["shares_diluted_weighted"] == 10
    assert a["periods"][0]["end"] == end and a["prov"]["revenue"][end]["accn"] == "EV-T p.1"


COMPANY_HTML = """
<div id="quote"><div>Example Motors Limited</div><div>AUTOMOBILE ASSEMBLER</div><div>Rs.1,394.80</div>
<div>As of Mon, Nov 2, 2026 3:49 PM</div><div>P/E Ratio (TTM) ** 3.63</div></div>
<div id="profile"><div>BUSINESS DESCRIPTION Makes cars. KEY PEOPLE x</div><div>WEBSITE www.example.com</div>
<div>REGISTRAR R AUDITOR A & Co. Fiscal Year End June</div></div>
<div id="equity"><div>Market Cap (000's) 84,310,026.40</div><div>Shares 60,445,961</div><div>Face Value 10.00</div>
<div>Free Float 21,156,087 Free Float 35.00%</div></div>
<div id="announcements"><table><tr><td>Sep 4, 2026</td><td>Transmission of Annual Report for the Year Ended June 30, 2026</td>
<td><a href="/download/document/282274.pdf">PDF</a></td></tr></table></div>
<div id="financials"><table><tr><th></th><th>2026</th><th>2025</th></tr><tr><td>Sales</td><td>191,715,354</td><td>108,694,500</td></tr>
<tr><td>EPS</td><td>390.51</td><td>270.26</td></tr></table></div>
<div id="ratios"></div>"""


def test_parse_company_page():
    i = psx.parse_company_page(COMPANY_HTML)
    assert i["name"] == "Example Motors Limited" and i["sector"] == "AUTOMOBILE ASSEMBLER" and i["price"] == 1394.8
    assert i["price_time"] == "2026-11-02T15:49:00+05:00" and i["shares"] == 60445961 and i["free_float_pct"] == 35.0
    assert i["announcements"][0]["urls"] == ["https://dps.psx.com.pk/download/document/282274.pdf"]
    assert i["vendor_financials"]["2026"]["Sales"] == 191715354 and i["vendor_financials"]["2025"]["EPS"] == 270.26
    docs = psx.pick_documents(i["announcements"], {"annual_report": "transmission of annual report", "quarterly_report": "q",
                                                   "results": "r", "briefing": "b", "operating_data": "zz", "material_information": "zz"})
    assert docs["annual_report"]["date"] == "2026-09-04"


GIS_HTML = "".join(f"<tr><td>{c}</td><td>{t}</td><td>{a}</td><td>{i}</td><td>{m}</td>" + "<td>1</td>" * 7 + f"<td>{y}</td><td>Download</td></tr>"
                   for c, t, a, i, m, y in [
                       ("P10FRZ050836", "Fixed Rate - Zero Coupon", "Sep 18, 2026", "Sep 19, 2026", "Sep 19, 2036", "12.5000"),
                       ("P05FRR081031", "Fixed Rental Rate (FRR)", "Oct 7, 2026", "Oct 8, 2026", "Oct 8, 2031", "12.8300"),
                       ("P10VRR151036", "Variable Rental Rate (VRR)", "Oct 28, 2026", "Oct 15, 2026", "Oct 15, 2036", "12.4104"),
                       ("P01GHS281027", "Fixed Rate Ijarah Sukuk issued at Discount", "Oct 28, 2026", "Oct 29, 2026", "Oct 28, 2027", "12.8955")])


def test_gis_parse_prefers_longest_fixed_tenor():
    rows = psx.parse_gis_auctions(GIS_HTML)
    assert len(rows) == 4 and not next(r for r in rows if "VRR" in r["code"])["fixed"]
    fixed_long = [r for r in rows if r["fixed"] and r["tenor_years"] >= 3]
    best = max(fixed_long, key=lambda r: r["tenor_years"])
    assert best["code"] == "P10FRZ050836" and best["cut_off_yield"] == pytest.approx(0.125)


def test_damodaran_country_row():
    html = ("<p>Last updated: January 5, 2026</p><table><tr><td>Pakistan</td><td>Caa1</td><td>6.37%</td><td>9.71%</td>"
            "<td>13.94%</td><td>29.00%</td></tr></table>")
    r = damodaran.parse_ctryprem(html, "Pakistan")
    assert r["rating"] == "Caa1" and r["default_spread"] == pytest.approx(0.0637) and r["crp"] == pytest.approx(0.0971)
    assert r["as_of"] == "2026-01-05"
    assert damodaran.relever(1.2, 0.1, 0.39) == pytest.approx(1.2 * (1 + 0.61 * 0.1))
