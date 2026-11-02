"""Manifest linking every final deliverable, plus a zip bundle."""
from __future__ import annotations

import zipfile
from pathlib import Path

from ..util import sha256_bytes, utcnow, write_json


def write_manifest(pl) -> dict:
    st = pl.state
    info = st.get("render_info")
    paths = {k: Path(v) for k, v in info["paths"].items()}
    gates = st.get("gates") or []
    desc = {"report_pdf": "Institutional equity research report (PDF)", "summary_pdf": "Investment summary (PDF, 1-2 pages)",
            "summary_md": "Investment summary (Markdown)", "model_xlsx": "Financial and valuation model (Excel, native formulas)",
            "sources_csv": "Research source register (CSV)", "sources_json": "Research source register (JSON)",
            "supporting_json": "Supporting data: statements, drivers, assumptions, valuation, synthesis, critique (JSON)",
            "validation": "Validation report: quality gates, workbook recalculation, report checks (JSON)"}
    files = []
    for k, p in paths.items():
        if k == "manifest" or not p.exists():
            continue
        b = p.read_bytes()
        files.append({"key": k, "description": desc.get(k, k), "file": p.name, "bytes": len(b), "sha256": sha256_bytes(b)})
    sec = st.get("security")
    rec = st.get("valuation")["reconciled"]
    syn = st.get("synthesis")
    manifest = {
        "run_id": st.run_dir.name, "security": {k: sec.get(k) for k in ("name", "ticker", "exchange", "cik")},
        "information_cutoff": st.data["cutoff"], "generated_at": utcnow().isoformat(), "label": st.data.get("label"),
        "rating": info["rating"], "fair_value": rec.get("fair_value"), "range": [rec.get("range_low"), rec.get("range_high")],
        "target_12m": rec.get("target_12m"), "confidence": syn.get("confidence"), "model_provider": syn.get("_provider"),
        "methodology": st.get("plan")["methodology"],
        "gates": [{k: g[k] for k in ("id", "name", "severity", "status")} for g in gates],
        "files": files, "run_folder_extras": {"evidence_raw": "sources/raw/", "excerpts": "sources/excerpts/",
                                              "checkpoints": "work/", "page_images": "validation/png/", "log": "run.log"},
    }
    write_json(paths["manifest"], manifest)
    bundle = paths["manifest"].parent / f"{sec['ticker']}_research_bundle.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(paths[f["key"]], f["file"])
        z.write(paths["manifest"], "manifest.json")
    st.put("manifest", manifest)
    return manifest
