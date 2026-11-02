"""Command-line entry point.

  eqr run AAPL                       # ticker; exchange optional (AAPL:NASDAQ)
  eqr resume runs/AAPL-20261102-...  # continue a paused/failed run
  eqr status <run_dir>               # stages, label, pause reason
  eqr requests <run_dir>             # blocked retrievals + model hand-offs
  eqr doctor                         # detect environment capabilities
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import pipeline
from .config import load_config
from .job import RunState
from .util import read_json, setup_logging

EXIT = {"complete": 0, "awaiting_evidence": 3, "awaiting_model": 4, "needs_input": 5, "budget_stop": 6, "failed": 1}


def _summary(st: RunState) -> None:
    d = st.data
    print(f"\nRun: {st.run_dir}\nStatus: {d['status']}   Label: {d.get('label', '-')}")
    if d.get("pause"):
        print(f"Paused at '{d['pause']['stage']}': {d['pause']['message']}")
        det = d["pause"].get("detail")
        if isinstance(det, dict) and det.get("requests"):
            print("Blocked sources (place files in the evidence inbox, or allow the hosts, then `eqr resume`):")
            for r in det["requests"]:
                print(f"  - {r['url']}\n      -> evidence_inbox/{st.data['ticker_input'].upper()}/{r['inbox_filename']}  ({r['last_error']})")
        if isinstance(det, list):
            for c in det:
                print(f"  candidate: {c}")
    m = read_json(st.run_dir / "deliverables" / "manifest.json")
    if m:
        print("Deliverables:")
        for f in m["files"]:
            print(f"  {st.run_dir / 'deliverables' / f['file']}")
        print(f"  {st.run_dir / 'deliverables' / 'manifest.json'}")


def cmd_run(a) -> int:
    opts = {"cutoff": a.cutoff, "fixtures": a.fixtures, "sector": a.sector, "cik": a.cik,
            "peers": [p.strip().upper() for p in a.peers.split(",")] if a.peers else None,
            "config_overrides": {"model": {"provider": a.provider}} if a.provider else None}
    ticker = f"{a.ticker}:{a.exchange}" if a.exchange else a.ticker
    st = pipeline.start(ticker, config=a.config, options=opts, verbose=a.verbose)
    try:
        status = pipeline.run(st, a.config, until=a.until)
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}. State saved; fix and run `eqr resume {st.run_dir}`.", file=sys.stderr)
        _summary(RunState(st.run_dir))
        return 1
    _summary(RunState(st.run_dir))
    return EXIT.get(status, 0)


def cmd_resume(a) -> int:
    st = RunState(Path(a.run_dir).resolve())
    setup_logging(a.verbose, st.run_dir / "run.log")
    if a.from_stage:
        st.reset_from(a.from_stage)
    else:
        stage = st.first_incomplete()
        if stage:
            st.data["stages"][stage] = {"status": "pending"}
    try:
        status = pipeline.run(st, a.config)
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}", file=sys.stderr)
        _summary(RunState(st.run_dir))
        return 1
    _summary(RunState(st.run_dir))
    return EXIT.get(status, 0)


def cmd_status(a) -> int:
    st = RunState(Path(a.run_dir))
    for s, v in st.data["stages"].items():
        print(f"  {s:<10} {v['status']}{'  ' + v.get('message', '') if v.get('message') else ''}")
    print("  counters:", st.data["counters"])
    _summary(st)
    return 0


def cmd_requests(a) -> int:
    run = Path(a.run_dir)
    reqs = read_json(run / "work" / "retrieval_requests.json", [])
    st = RunState(run)
    print("Retrieval requests:")
    for r in reqs:
        print(f"  [{r['status']}] {r['url']}\n      inbox file: evidence_inbox/{st.data['ticker_input'].upper()}/{r['inbox_filename']}")
    md = run / "work" / "model_requests"
    if md.exists():
        print("Model hand-offs:")
        for p in sorted(md.glob("*.json")):
            resp = run / "work" / "model_responses" / p.name
            print(f"  {p}  ->  {resp}  [{'answered' if resp.exists() else 'pending'}]")
    return 0


def cmd_doctor(a) -> int:
    import os
    import requests
    cfg = load_config(a.config)
    hosts = ["https://www.sec.gov/files/company_tickers_exchange.json", "https://data.sec.gov/submissions/CIK0000320193.json",
             "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=5d&interval=1d", "https://stooq.com/q/d/l/?s=spy.us&i=d",
             "https://home.treasury.gov/", "https://pages.stern.nyu.edu/~adamodar/"]
    from .config import user_agent
    out = {"network": {}, "libreoffice": shutil.which("soffice") or None, "pdftoppm": shutil.which("pdftoppm") or None,
           "model_credentials": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
           "sec_user_agent_set": bool(os.environ.get(cfg["http"]["user_agent_env"]))}
    for h in hosts:
        try:
            r = requests.get(h, timeout=10, headers={"User-Agent": user_agent(cfg)})
            out["network"][h] = r.status_code
        except Exception as e:  # noqa: BLE001
            out["network"][h] = f"unreachable ({type(e).__name__})"
    print(json.dumps(out, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="eqr", description="Autonomous institutional equity research platform")
    p.add_argument("--config", help="YAML config overriding config/default.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="research a ticker end to end")
    r.add_argument("ticker")
    r.add_argument("--exchange")
    r.add_argument("--cutoff", help="information cutoff, ISO datetime (default: now, UTC)")
    r.add_argument("--provider", choices=["anthropic", "agent_handoff", "none", "auto"])
    r.add_argument("--peers", help="comma-separated peer tickers (default: sector playbook)")
    r.add_argument("--sector", help="force a sector playbook id (methodology/playbooks/sectors)")
    r.add_argument("--cik", help="force SEC CIK (resolves ambiguity)")
    r.add_argument("--until", help="stop after this stage")
    r.add_argument("--fixtures", help=argparse.SUPPRESS)  # offline SYNTHETIC test mode
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("resume")
    s.add_argument("run_dir")
    s.add_argument("--from-stage")
    s.set_defaults(fn=cmd_resume)
    for name, fn in (("status", cmd_status), ("requests", cmd_requests)):
        x = sub.add_parser(name)
        x.add_argument("run_dir")
        x.set_defaults(fn=fn)
    d = sub.add_parser("doctor")
    d.set_defaults(fn=cmd_doctor)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
