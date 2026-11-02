"""Pakistan Stock Exchange adapter (dps.psx.com.pk public pages).

Only public HTML pages and document downloads are used. PSX's AJAX/JSON
endpoints answer 403 to non-browser clients; the platform does not attempt to
bypass that (see methodology/markets/psx.yaml)."""
from __future__ import annotations

import datetime as dt
import html
import re

from .evidence import EvidenceStore

BASE = "https://dps.psx.com.pk"


def _text(fragment: str) -> str:
    s = re.sub(r"(?s)<script.*?</script>", " ", fragment)
    s = re.sub(r"<(td|th)[^>]*>", " | ", s)
    s = re.sub(r"</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _section(page: str, sec_id: str) -> str:
    ids = ["quote", "profile", "equity", "announcements", "financials", "ratios", "payouts", "reports"]
    m = re.search(r'id="%s"(.*?)(?=<div[^>]+id="(?:%s)"|$)' % (sec_id, "|".join(i for i in ids if i != sec_id)), page, re.S)
    return m.group(1) if m else ""


def _num(s: str) -> float | None:
    s = s.strip().replace(",", "")
    neg = s.startswith("(") or s.startswith("-")
    s = s.strip("()-% ")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_company_page(page: str) -> dict:
    out: dict = {}
    q = _text(_section(page, "quote"))
    m = re.search(r"^>?\s*(.+?)\s+([A-Z][A-Z &/\-]+?)\s+Rs\.\s*([\d,]+\.\d+)", q)
    if not m:
        raise ValueError("company page: quote block not recognised (symbol may not exist)")
    out["name"], out["sector"], out["price"] = m.group(1).strip(), m.group(2).strip(), _num(m.group(3))
    t = re.search(r"As of ([A-Za-z]{3}, [A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)", q)
    if t:
        local = dt.datetime.strptime(t.group(1), "%a, %b %d, %Y %I:%M %p")
        out["price_time"] = local.replace(tzinfo=dt.timezone(dt.timedelta(hours=5))).isoformat()  # PKT, UTC+5
    for key, pat in (("pe_ttm", r"P/E Ratio \(TTM\)\s*\**\s*([\d,\.]+)"), ("ldcp", r"LDCP\s*([\d,\.]+)"),
                     ("high_52w", r"52-WEEK RANGE\s*\^?\s*[\d,\.]+\s*—\s*([\d,\.]+)"),
                     ("low_52w", r"52-WEEK RANGE\s*\^?\s*([\d,\.]+)"), ("volume", r"Volume\s*([\d,]+)")):
        mm = re.search(pat, q)
        if mm:
            out[key] = _num(mm.group(1))
    p = _text(_section(page, "profile"))
    for key, pat in (("business_description", r"BUSINESS DESCRIPTION\s*(.+?)\s*KEY PEOPLE"), ("website", r"WEBSITE\s*(\S+)"),
                     ("auditor", r"AUDITOR\s*(.+?)\s*Fiscal Year End"), ("fiscal_year_end", r"Fiscal Year End\s*(\w+)"),
                     ("registrar", r"REGISTRAR\s*(.+?)\s*AUDITOR")):
        mm = re.search(pat, p, re.S)
        if mm:
            out[key] = re.sub(r"\s+", " ", mm.group(1)).strip()
    e = _text(_section(page, "equity"))
    for key, pat in (("market_cap_000", r"Market Cap \(000'?\s*s\)\s*([\d,\.]+)"), ("shares", r"Shares\s*([\d,]+)"),
                     ("face_value", r"Face Value\s*([\d\.]+)"), ("free_float_shares", r"Free Float\s*([\d,]+)\s*Free Float"),
                     ("free_float_pct", r"Free Float\s*([\d\.]+)%")):
        mm = re.search(pat, e)
        if mm:
            out[key] = _num(mm.group(1))
    # announcements: rows with date, title and document links
    ann = []
    for row in re.findall(r"<tr>(.*?)</tr>", _section(page, "announcements"), re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        links = [l for l in re.findall(r'href="(/download/[^"]+)"', row)]
        if len(cells) >= 2 and re.match(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", cells[0]):
            ann.append({"date": dt.datetime.strptime(cells[0], "%b %d, %Y").date().isoformat(), "title": cells[1],
                        "urls": [BASE + l for l in links]})
    out["announcements"] = ann
    # vendor summary financials (Capital Stake): cross-check only
    f = _text(_section(page, "financials"))
    vend: dict = {}
    hdr: list = []
    for line in f.split("\n"):
        parts = [x.strip() for x in line.split("|") if x.strip()]
        if not parts:
            continue
        yrs = [p for p in parts if re.fullmatch(r"(Q\d )?\d{4}", p)]
        if len(yrs) >= 2 and len(yrs) >= len(parts) - 1:     # header row (may carry a caption cell)
            hdr = yrs
            continue
        if hdr and len(parts) == len(hdr) + 1:
            for h, v in zip(hdr, parts[1:]):
                vend.setdefault(h, {})[parts[0]] = _num(v)
    out["vendor_financials"] = vend
    return out


def company(store: EvidenceStore, symbol: str) -> tuple[dict, str]:
    url = f"{BASE}/company/{symbol.upper()}"
    data, ev = store.fetch(url, kind="psx_company_page", title=f"PSX data portal - company page {symbol.upper()}",
                           publisher="Pakistan Stock Exchange (quote: exchange; summary financials: Capital Stake)", tier=1,
                           ttl_hours=2, critical=True, purpose="Security identification, quote, share count, filings list")
    info = parse_company_page(data.decode("utf-8", errors="ignore"))
    ev.period = info.get("price_time")
    store.save()
    return info, ev.id


def pick_documents(announcements: list[dict], patterns: dict) -> dict:
    """Most recent announcement (with a PDF) per document type."""
    out = {}
    for kind, pat in patterns.items():
        for a in sorted(announcements, key=lambda x: x["date"], reverse=True):
            if re.search(pat, a["title"], re.I) and a["urls"]:
                out[kind] = a
                break
    out["all_operating_data"] = [a for a in announcements if re.search(patterns["operating_data"], a["title"], re.I) and a["urls"]]
    out["all_material_information"] = [a for a in announcements if re.search(patterns["material_information"], a["title"], re.I) and a["urls"]]
    return out


def document(store: EvidenceStore, ann: dict, kind: str, title: str, tier: int = 1) -> tuple[bytes, str]:
    url = ann["urls"][-1]
    data, ev = store.fetch(url, kind=f"psx_{kind}", title=f"{title}: {ann['title']} ({ann['date']})",
                           publisher="Issuer via Pakistan Stock Exchange", tier=tier, ttl_hours=8760,
                           purpose=f"Primary document ({kind})", critical=kind in ("annual_report",))
    ev.published_at = ann["date"]
    store.save()
    return data, ev.id


def parse_gis_auctions(page: str) -> list[dict]:
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 14 or not re.match(r"P\d\d", cells[0]):
            continue
        try:
            auction = dt.datetime.strptime(cells[2], "%b %d, %Y").date()
            issue = dt.datetime.strptime(cells[3], "%b %d, %Y").date()
            maturity = dt.datetime.strptime(cells[4], "%b %d, %Y").date()
            yld = float(cells[-2].replace(",", ""))
        except ValueError:
            continue
        rows.append({"code": cells[0], "type": cells[1], "auction_date": auction.isoformat(), "issue_date": issue.isoformat(),
                     "maturity": maturity.isoformat(), "tenor_years": round((maturity - issue).days / 365.25, 1),
                     "cut_off_yield": yld / 100, "fixed": not re.search(r"variable|VRR", cells[1], re.I)})
    return rows


def gis_risk_free(store: EvidenceStore, cutoff: dt.date, min_tenor: float = 3.0) -> dict | None:
    url = f"{BASE}/gis-auction-results"
    data, ev = store.fetch(url, kind="psx_gis_auctions", title="PSX - GoP Ijarah Sukuk auction results",
                           publisher="Pakistan Stock Exchange / Government of Pakistan", tier=2, ttl_hours=12, critical=True,
                           purpose="PKR government yield for the risk-free rate")
    rows = [r for r in parse_gis_auctions(data.decode("utf-8", errors="ignore"))
            if r["fixed"] and r["auction_date"] <= cutoff.isoformat()]
    if not rows:
        return None
    long_ = [r for r in rows if r["tenor_years"] >= min_tenor and r["cut_off_yield"] > 0]
    pool = long_ or rows
    latest = max(r["auction_date"] for r in pool)
    recent = [r for r in pool if (dt.date.fromisoformat(latest) - dt.date.fromisoformat(r["auction_date"])).days <= 90]
    best = max(recent, key=lambda r: (r["tenor_years"], r["auction_date"]))
    short = [r for r in rows if r["tenor_years"] <= 1.1]
    short_best = max(short, key=lambda r: r["auction_date"]) if short else None
    ev.period = best["auction_date"]
    store.save()
    return {"gov_yield": best["cut_off_yield"], "instrument": f"{best['code']} {best['type']} ({best['tenor_years']}y)",
            "as_of": best["auction_date"], "short_yield": short_best["cut_off_yield"] if short_best else None,
            "short_instrument": f"{short_best['code']} ({short_best['tenor_years']}y)" if short_best else None,
            "evidence_id": ev.id}
