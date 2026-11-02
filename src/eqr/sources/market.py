"""Market price adapters (tier 4 aggregators, timestamped, cross-checked when
two sources are reachable). Prices are never invented: if no source is
reachable the market-price gate fails and price-relative conclusions are
withheld."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json

from .evidence import EvidenceStore, SourceUnavailable

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={iv}&includeAdjustedClose=true"
STOOQ_DAILY = "https://stooq.com/q/d/l/?s={sym}&i={iv}"


def yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-")


def stooq_symbol(ticker: str, country: str = "us") -> str:
    return f"{ticker.lower().replace('.', '-')}.{country}"


def parse_yahoo(data: bytes) -> dict:
    js = json.loads(data)["chart"]["result"][0]
    meta = js["meta"]
    ts = js.get("timestamp") or []
    q = js["indicators"]["quote"][0]
    adj = (js["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q.get("close")
    series = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), c, a)
              for t, c, a in zip(ts, q.get("close"), adj) if c is not None]
    return {
        "price": meta.get("regularMarketPrice"),
        "price_time": dt.datetime.fromtimestamp(meta["regularMarketTime"], dt.timezone.utc).isoformat()
        if meta.get("regularMarketTime") else None,
        "currency": meta.get("currency"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "series": [{"date": d, "close": c, "adj_close": a} for d, c, a in series],
    }


def parse_stooq(data: bytes) -> dict:
    text = data.decode("utf-8", errors="ignore")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Close" not in rows[0]:
        raise ValueError("unexpected Stooq payload (possibly blocked or symbol unknown)")
    series = [{"date": r["Date"], "close": float(r["Close"]), "adj_close": float(r["Close"])} for r in rows if r.get("Close")]
    last = series[-1]
    return {"price": last["close"], "price_time": last["date"] + "T21:00:00+00:00 (daily close)",
            "currency": None, "exchange": None, "series": series}


def price_history(store: EvidenceStore, ticker: str, *, monthly: bool, critical: bool, label: str) -> tuple[dict, str] | None:
    """Try Yahoo then Stooq. Returns (parsed, evidence_id) or None."""
    attempts = [
        (YAHOO_CHART.format(sym=yahoo_symbol(ticker), rng="6y", iv="1mo" if monthly else "1d"), parse_yahoo, "Yahoo Finance chart API"),
        (STOOQ_DAILY.format(sym=stooq_symbol(ticker), iv="m" if monthly else "d"), parse_stooq, "Stooq"),
    ]
    for url, parser, pub in attempts:
        try:
            data, ev = store.fetch(url, kind="market_prices", title=f"{label} {'monthly' if monthly else 'daily'} prices ({ticker})",
                                   publisher=pub, tier=4, ttl_hours=6, critical=critical,
                                   purpose="Reference price / beta regression")
            parsed = parser(data)
            ev.period = parsed["series"][-1]["date"] if parsed["series"] else None
            store.save()
            return parsed, ev.id
        except SourceUnavailable:
            continue
        except (ValueError, KeyError, IndexError, TypeError) as e:
            store.mark_invalid(url, "market_prices", f"content failed validation ({type(e).__name__}): not price data "
                                                     f"(e.g. a bot-check page); not used")
            continue
    return None


def reference_price(store: EvidenceStore, ticker: str) -> dict | None:
    """Reference price with timestamp, cross-checked across sources when possible."""
    got = []
    for monthly in (False,):
        r = price_history(store, ticker, monthly=monthly, critical=True, label="Reference")
        if r:
            got.append(r)
    if not got:
        return None
    parsed, eid = got[0]
    out = {"price": parsed["price"], "price_time": parsed["price_time"], "currency": parsed["currency"],
           "exchange": parsed["exchange"], "evidence_id": eid, "cross_check": None}
    return out


def monthly_returns(series: list[dict]) -> dict[str, float]:
    """Month-end adjusted-close returns keyed by YYYY-MM."""
    by_month: dict[str, float] = {}
    for p in series:
        if p.get("adj_close") is None:
            continue
        by_month[p["date"][:7]] = p["adj_close"]  # last obs in month wins
    keys = sorted(by_month)
    return {k: by_month[k] / by_month[p] - 1 for p, k in zip(keys, keys[1:]) if by_month[p]}
