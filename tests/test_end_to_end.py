"""End-to-end on the SYNTHETIC fixture (offline): every stage, workbook
recalculation in LibreOffice, report-vs-workbook checks, model hand-off and
resume. The synthetic run must never be labelled FINAL_VALIDATED."""
import json
import shutil
from pathlib import Path

import pytest

from eqr import pipeline
from eqr.job import RunState
from eqr.util import read_json

FIX = Path(__file__).parent / "fixtures" / "synthetic"
CUT = "2026-11-02T12:00:00+00:00"


def _cfg(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(f"runs_dir: {tmp_path / 'runs'}\ninbox_dir: {tmp_path / 'inbox'}\ncache_dir: {tmp_path / 'cache'}\n")
    return str(p)


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")
def test_synthetic_end_to_end_with_handoff(tmp_path):
    cfg = _cfg(tmp_path)
    st = pipeline.start("SYN", config=cfg, options={"fixtures": str(FIX), "cutoff": CUT,
                                                    "config_overrides": {"model": {"provider": "agent_handoff"}}})
    status = pipeline.run(st, cfg)
    assert status == "awaiting_model"
    req = read_json(st.work / "model_requests" / "synthesis.json")
    assert "<evidence>" in req["prompt"] and req["schema"]["required"]
    # an external agent answers (here: a minimal, schema-valid, evidence-cited answer)
    from eqr import analysis as AN
    ctxd = pipeline.Pipeline(RunState(st.run_dir), pipeline.load_config(cfg, {"model": {"provider": "agent_handoff"}}))._ctx()
    ans = AN.deterministic_synthesis(ctxd)
    ans.pop("_generated_by")
    ans.update(view="NEUTRAL", confidence="MEDIUM", thesis_pillars=[{"title": "Cash generation", "argument": "Test pillar.",
                                                                       "evidence_ids": ["EV-0003"]}])
    (st.work / "model_responses").mkdir(exist_ok=True)
    (st.work / "model_responses" / "synthesis.json").write_text(json.dumps(ans))
    st2 = RunState(st.run_dir)
    st2.data["stages"]["synthesis"] = {"status": "pending"}
    status = pipeline.run(st2, cfg)
    assert status == "awaiting_model"                                   # critique hand-off next
    (st.work / "model_responses" / "critique.json").write_text(json.dumps({"overall": "ok", "findings": []}))
    st3 = RunState(st.run_dir)
    st3.data["stages"]["critique"] = {"status": "pending"}
    assert pipeline.run(st3, cfg) == "complete"
    st4 = RunState(st.run_dir)
    gates = {g["id"]: g for g in st4.get("gates")}
    assert gates["G12"]["status"] == "VERIFIED"          # LibreOffice recalculation == engine
    assert gates["G13"]["status"] == "VERIFIED"
    assert gates["G14"]["status"] == "VERIFIED"          # report numbers == workbook
    assert gates["G07"]["status"] == "VERIFIED" and gates["G04"]["status"] == "VERIFIED"
    assert gates["G19"]["status"] == "VERIFIED"
    assert gates["G02"]["status"] == "FAILED"            # synthetic data is never traceable
    assert st4.data["label"] == "BLOCKED_PARTIAL"
    man = read_json(st.run_dir / "deliverables" / "manifest.json")
    keys = {f["key"] for f in man["files"]}
    assert {"report_pdf", "summary_pdf", "summary_md", "model_xlsx", "sources_csv", "validation"} <= keys
    assert (st.run_dir / "deliverables" / "SYN_research_bundle.zip").exists()
    md = (st.run_dir / "deliverables" / "SYN_investment_summary.md").read_text()
    assert "SYNTHETIC TEST DATA" in md
    from openpyxl import load_workbook
    wb = load_workbook(st.run_dir / "deliverables" / "SYN_financial_model.xlsx")
    formulas = [c.value for row in wb["DCF"].iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert len(formulas) > 50                              # native formulas, not pasted values
    assert not any(ws.sheet_state != "visible" for ws in wb.worksheets)
