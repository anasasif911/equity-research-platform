"""SEC EDGAR adapter (tier 1). Covers US domestic filers and foreign private
issuers that file 10-K/20-F/40-F with XBRL (US GAAP or IFRS)."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

from .evidence import EvidenceStore, SourceUnavailable

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"

EXCHANGE_ALIASES = {
    "NASDAQ": "Nasdaq", "XNAS": "Nasdaq", "NMS": "Nasdaq", "NYSE": "NYSE", "XNYS": "NYSE",
    "ARCA": "NYSE", "NYSEARCA": "NYSE", "AMEX": "NYSE", "CBOE": "CBOE", "OTC": "OTC",
}


@dataclass
class Candidate:
    cik: int
    name: str
    ticker: str
    exchange: str


def parse_ticker_input(s: str) -> tuple[str, str | None]:
    """'AAPL', 'AAPL:NASDAQ', 'NASDAQ:AAPL', 'AAPL.US', 'BRK.B' -> (ticker, exchange|None)."""
    s = s.strip().upper()
    if ":" in s:
        a, b = s.split(":", 1)
        if a in EXCHANGE_ALIASES or a in {"PSX", "NSE", "BSE", "LSE", "TSX", "ASX", "HKEX", "XETRA"}:
            return b, a
        return a, b
    if s.endswith(".US"):
        return s[:-3], "US"
    return s, None


def resolve(store: EvidenceStore, ticker_input: str) -> dict:
    ticker, exch = parse_ticker_input(ticker_input)
    data, ev = store.fetch(TICKERS_URL, kind="sec_ticker_map", title="SEC company tickers and exchanges",
                           publisher="U.S. SEC", tier=1, ttl_hours=24, critical=True,
                           purpose="Authoritative ticker -> CIK -> exchange resolution")
    js = json.loads(data)
    fields = js["fields"]
    rows = [dict(zip(fields, r)) for r in js["data"]]
    norm = ticker.replace(".", "-")
    cands = [Candidate(int(r["cik"]), r["name"], r["ticker"], r["exchange"] or "")
             for r in rows if (r["ticker"] or "").upper() in (ticker, norm)]
    if exch and exch != "US":
        want = EXCHANGE_ALIASES.get(exch, exch)
        filt = [c for c in cands if c.exchange.upper() == want.upper()]
        if not filt and cands:
            return {"status": "ambiguous", "evidence_id": ev.id,
                    "question": f"'{ticker}' is listed on {', '.join(c.exchange for c in cands)} per SEC, not {exch}. Which security did you mean?",
                    "candidates": [c.__dict__ for c in cands]}
        cands = filt
    ciks = {c.cik for c in cands}
    if not cands:
        return {"status": "not_found", "evidence_id": ev.id, "ticker": ticker, "exchange": exch}
    if len(ciks) > 1:
        return {"status": "ambiguous", "evidence_id": ev.id,
                "question": f"Ticker '{ticker}' maps to several SEC registrants. Which one?",
                "candidates": [c.__dict__ for c in cands]}
    c = cands[0]
    return {"status": "resolved", "cik": c.cik, "name": c.name, "ticker": c.ticker, "exchange": c.exchange,
            "evidence_id": ev.id}


def submissions(store: EvidenceStore, cik: int) -> tuple[dict, str]:
    data, ev = store.fetch(SUBMISSIONS_URL.format(cik=cik), kind="sec_submissions",
                           title=f"EDGAR submissions index CIK {cik}", publisher="U.S. SEC", tier=1,
                           ttl_hours=12, critical=True, purpose="Filer profile, SIC, fiscal year end, filing list")
    return json.loads(data), ev.id


def companyfacts(store: EvidenceStore, cik: int) -> tuple[dict, str]:
    data, ev = store.fetch(FACTS_URL.format(cik=cik), kind="sec_companyfacts",
                           title=f"EDGAR XBRL company facts CIK {cik}", publisher="U.S. SEC", tier=1,
                           ttl_hours=24, critical=True, purpose="All XBRL financial facts from 10-K/10-Q/20-F filings")
    return json.loads(data), ev.id


def recent_filings(subs: dict) -> list[dict]:
    r = subs["filings"]["recent"]
    keys = list(r.keys())
    out = [dict(zip(keys, vals)) for vals in zip(*[r[k] for k in keys])]
    return out


def latest_filing(subs: dict, forms: tuple[str, ...]) -> dict | None:
    for f in recent_filings(subs):
        if f["form"] in forms:
            return f
    return None


def _html_to_text(raw: bytes) -> str:
    s = raw.decode("utf-8", errors="ignore")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s


SECTION_PATTERNS = {
    "business": (r"item\s*1\.?\s*business", r"item\s*1a\.?\s*risk\s*factors"),
    "risk_factors": (r"item\s*1a\.?\s*risk\s*factors", r"item\s*1b\.?|item\s*1c\.?|item\s*2\.?\s*properties"),
    "legal_proceedings": (r"item\s*3\.?\s*legal\s*proceedings", r"item\s*4\.?"),
    "mdna": (r"item\s*7\.?\s*management.s\s*discussion", r"item\s*7a\.?|item\s*8\.?"),
    "market_risk": (r"item\s*7a\.?\s*quantitative", r"item\s*8\.?"),
}


def extract_sections(text: str, max_chars: int = 60000) -> dict[str, str]:
    """Pull 10-K sections. The table of contents also matches the headings, so
    we take the LONGEST span between a start match and the next end match."""
    out = {}
    low = text.lower()
    for name, (start, end) in SECTION_PATTERNS.items():
        best = ""
        for m in re.finditer(start, low):
            e = re.search(end, low[m.end():])
            if not e:
                continue
            span = text[m.start(): m.end() + e.start()]
            if len(span) > len(best):
                best = span
        if best:
            out[name] = best.strip()[:max_chars]
    return out


def filing_document(store: EvidenceStore, cik: int, filing: dict, kind: str = "sec_filing_document") -> tuple[str, str]:
    acc = filing["accessionNumber"].replace("-", "")
    url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=filing["primaryDocument"])
    data, ev = store.fetch(url, kind=kind, title=f"{filing['form']} filed {filing['filingDate']} ({filing['accessionNumber']})",
                           publisher="U.S. SEC / issuer", tier=1, ttl_hours=8760,
                           purpose="Narrative disclosures: business, risk factors, MD&A, legal proceedings")
    ev.published_at = filing["filingDate"]
    ev.period = filing.get("reportDate")
    store.save()
    return _html_to_text(data), ev.id


def earnings_release_after(store: EvidenceStore, cik: int, subs: dict, after_date: str) -> tuple[str, str, dict] | None:
    """Most recent 8-K Item 2.02 (results of operations) filed after the latest
    periodic report: captures results announced but not yet in XBRL filings."""
    for f in recent_filings(subs):
        if f["form"] == "8-K" and "2.02" in (f.get("items") or "") and f["filingDate"] > after_date:
            acc = f["accessionNumber"].replace("-", "")
            try:
                idx_raw, _ = store.fetch(INDEX_URL.format(cik=cik, acc=acc), kind="sec_filing_index",
                                         title=f"8-K index {f['accessionNumber']}", publisher="U.S. SEC", tier=1,
                                         ttl_hours=8760, purpose="Locate earnings-release exhibit")
                items = json.loads(idx_raw)["directory"]["item"]
                ex = next((i["name"] for i in items if re.search(r"ex-?99|exhibit99|ex99", i["name"], re.I)
                           and i["name"].lower().endswith((".htm", ".html"))), None)
                if not ex:
                    return None
                url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=ex)
                data, ev = store.fetch(url, kind="sec_earnings_release",
                                       title=f"Earnings release (8-K Ex.99.1) filed {f['filingDate']}",
                                       publisher="Issuer via U.S. SEC", tier=1, ttl_hours=8760,
                                       purpose="Latest announced results not yet in periodic XBRL")
                ev.published_at = f["filingDate"]
                store.save()
                return _html_to_text(data), ev.id, f
            except SourceUnavailable:
                return None
    return None
