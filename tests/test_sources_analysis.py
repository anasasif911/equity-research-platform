import datetime as dt
import json

import pytest

from eqr import analysis as AN
from eqr.config import load_config
from eqr.llm import AgentHandoff, validate_schema
from eqr.job import Paused
from eqr.sources import rates, sec
from eqr.sources.evidence import EvidenceStore, SourceUnavailable, request_id_for


def test_parse_ticker_input():
    assert sec.parse_ticker_input("aapl") == ("AAPL", None)
    assert sec.parse_ticker_input("AAPL:NASDAQ") == ("AAPL", "NASDAQ")
    assert sec.parse_ticker_input("NASDAQ:AAPL") == ("AAPL", "NASDAQ")
    assert sec.parse_ticker_input("PSX:ENGRO") == ("ENGRO", "PSX")


def test_treasury_parse_uses_latest_on_or_before_cutoff(tmp_path):
    csv = b"Date,1 Yr,10 Yr\n10/31/2026,3.9,4.25\n10/30/2026,3.8,4.20\n11/03/2026,3.7,4.10\n"
    r = rates.parse_treasury_csv(csv, dt.date(2026, 11, 2))
    assert r["as_of"] == "2026-10-31" and r["value"] == pytest.approx(0.0425) and r["curve"]["1 Yr"] == pytest.approx(0.039)


def test_damodaran_erp_parse():
    html = "<p>Implied ERP on October 1, 2026 = 4.33%</p>"
    r = rates.parse_damodaran_erp(html)
    assert r["value"] == pytest.approx(0.0433) and r["as_of"] == "2026-10-01"
    with pytest.raises(ValueError):
        rates.parse_damodaran_erp("no premium here")


def _store(tmp_path, ticker="TST"):
    cfg = load_config(overrides={"inbox_dir": str(tmp_path / "inbox"), "cache_dir": str(tmp_path / "cache"),
                                 "http": {"retry": {"max_attempts": 1, "backoff_s": [0]}, "timeout_s": 3}})
    return EvidenceStore(tmp_path / "run", cfg, {"rate_limits_per_second": {"default": 50}}, ticker)


def test_blocked_fetch_is_recorded_and_inbox_fulfils_it(tmp_path):
    st = _store(tmp_path)
    url = "https://nonexistent.invalid/data.json"
    with pytest.raises(SourceUnavailable) as e:
        st.fetch(url, kind="test_kind", title="t", publisher="p", tier=1, critical=True)
    req = st.requests[e.value.request_id]
    assert req["status"] == "blocked" and req["critical"]
    # a human/agent supplies the document via the evidence inbox
    inbox = tmp_path / "inbox" / "TST"
    inbox.mkdir(parents=True)
    (inbox / req["inbox_filename"]).write_text('{"ok": 1}')
    (inbox / f"{request_id_for('test_kind', url)}.meta.json").write_text(json.dumps(
        {"url": url, "retrieved_at": "2026-11-02T10:00:00+00:00", "method": "manual download", "retrieved_by": "analyst"}))
    data, ev = st.fetch(url, kind="test_kind", title="t", publisher="p", tier=1)
    assert json.loads(data) == {"ok": 1} and ev.method == "inbox" and "manual download" in ev.notes
    assert st.requests[e.value.request_id]["status"] == "fulfilled_inbox"
    assert ev.sha256 and (tmp_path / "run" / ev.local_path).exists()


def test_resolve_ambiguity_asks(tmp_path):
    from pathlib import Path
    st = _store(tmp_path)
    st.fixtures = [(r"company_tickers_exchange", Path(__file__).parent / "fixtures" / "synthetic" / "tickers.json")]
    r = sec.resolve(st, "DUP")
    assert r["status"] == "ambiguous" and len(r["candidates"]) == 2
    assert sec.resolve(st, "DUP:NYSE")["status"] == "resolved"
    assert sec.resolve(st, "NOPE")["status"] == "not_found"


def test_filing_sections_extracted_from_longest_span():
    text = ("Table of contents Item 1. Business 3 Item 1A. Risk Factors 9 Item 2. Properties 20\n"
            "Item 1. Business\nWe make things. " + "x" * 500 + "\nItem 1A. Risk Factors\nRisks here." + "y" * 800 +
            "\nItem 1B. Unresolved\nItem 2. Properties\n")
    s = sec.extract_sections(text)
    assert "We make things" in s["business"] and "Risks here" in s["risk_factors"]


def test_citation_and_number_critic():
    pack = {"a": 1234.5, "b": 0.153}
    synth = {"headline": "Revenue 1,234.5 grew 15.3% while margin hit 99.9% [EV-0001] [EV-0099]", "thesis_pillars":
             [{"title": "x", "argument": "y", "evidence_ids": []}], "catalysts": []}
    f = AN.critique_numbers_and_citations(synth, pack, {"EV-0001"})
    probs = " | ".join(x["target"] + x["problem"] for x in f)
    assert "EV-0099" in probs                         # unknown evidence id -> critical
    assert "99.9%" in probs                           # number not in pack
    assert "15.3%" not in probs and "1,234.5" not in probs
    assert any(x["severity"] == "major" and "cites no evidence" in x["problem"] for x in f)


def test_agent_handoff_pauses_then_validates(tmp_path):
    h = AgentHandoff(tmp_path)
    schema = {"type": "object", "required": ["view"], "properties": {"view": {"type": "string", "enum": ["A", "B"]}}}
    with pytest.raises(Paused):
        h.complete("synthesis", "sys", "prompt", schema)
    assert (tmp_path / "model_requests" / "synthesis.json").exists()
    (tmp_path / "model_responses").mkdir()
    (tmp_path / "model_responses" / "synthesis.json").write_text('{"view": "C"}')
    with pytest.raises(Paused):                       # invalid answer is rejected, not trusted
        h.complete("synthesis", "sys", "prompt", schema)
    (tmp_path / "model_responses" / "synthesis.json").write_text('{"view": "A"}')
    assert h.complete("synthesis", "sys", "prompt", schema) == {"view": "A"}


def test_synthesis_schema_is_strict_compatible():
    def walk(s):
        if s.get("type") == "object":
            assert s.get("additionalProperties") is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(AN.SYNTHESIS_SCHEMA)
    walk(AN.CRITIQUE_SCHEMA)
