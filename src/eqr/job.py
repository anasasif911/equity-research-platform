"""Resumable run state. Each stage writes its outputs under runs/<id>/work/
and is marked complete in state.json only after its outputs are written
(atomic JSON writes). `eqr resume <run>` re-enters at the first incomplete
stage. Budgets (fetches, wall-clock, model calls, model cost) are enforced
here and always stop with a checkpoint rather than an abrupt failure."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .util import read_json, utcnow, write_json

STAGES = [
    "resolve", "plan", "retrieve", "extract", "normalize", "reconcile", "drivers",
    "forecast", "valuation", "synthesis", "critique", "revise", "render", "validate", "deliver",
]

# terminal / pause statuses
COMPLETE = "complete"
AWAITING_EVIDENCE = "awaiting_evidence"
AWAITING_MODEL = "awaiting_model"
NEEDS_INPUT = "needs_input"
BUDGET_STOP = "budget_stop"
FAILED = "failed"


class Paused(Exception):
    """Raised by a stage to checkpoint and pause the run (not an error)."""

    def __init__(self, status: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status, self.message, self.detail = status, message, detail


class RunState:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.path = run_dir / "state.json"
        self.data: dict = read_json(self.path, {})
        self._t0 = time.monotonic()

    @classmethod
    def create(cls, runs_dir: Path, ticker: str, options: dict) -> "RunState":
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        safe = "".join(c for c in ticker.upper() if c.isalnum() or c in "-_.")
        run_dir = Path(runs_dir) / f"{safe}-{stamp}"
        (run_dir / "work").mkdir(parents=True, exist_ok=True)
        st = cls(run_dir)
        st.data = {
            "ticker_input": ticker, "options": options, "created_at": utcnow().isoformat(),
            "cutoff": options.get("cutoff") or utcnow().isoformat(), "status": "running",
            "stages": {s: {"status": "pending"} for s in STAGES},
            "counters": {"fetches": 0, "llm_calls": 0, "llm_cost_usd": 0.0, "elapsed_s": 0.0},
            "events": [],
        }
        st.save()
        return st

    def save(self) -> None:
        self.data["counters"]["elapsed_s"] = round(
            self.data["counters"].get("elapsed_s_prev", 0.0) + (time.monotonic() - self._t0), 1)
        write_json(self.path, self.data)

    @property
    def work(self) -> Path:
        return self.run_dir / "work"

    def event(self, stage: str, msg: str, level: str = "info") -> None:
        self.data["events"].append({"t": utcnow().isoformat(), "stage": stage, "level": level, "msg": msg})
        self.save()

    def is_done(self, stage: str) -> bool:
        return self.data["stages"][stage]["status"] == COMPLETE

    def mark(self, stage: str, status: str, **info) -> None:
        self.data["stages"][stage] = {"status": status, "at": utcnow().isoformat(), **info}
        self.save()

    def first_incomplete(self) -> str | None:
        for s in STAGES:
            if not self.is_done(s):
                return s
        return None

    def reset_from(self, stage: str) -> None:
        hit = False
        for s in STAGES:
            hit = hit or s == stage
            if hit:
                self.data["stages"][s] = {"status": "pending"}
        self.save()

    # ---------------------------------------------------------------- budgets
    def begin_session(self) -> None:
        self.data["counters"]["elapsed_s_prev"] = self.data["counters"].get("elapsed_s", 0.0)
        self._t0 = time.monotonic()

    def count_fetch(self) -> None:
        self.data["counters"]["fetches"] += 1
        self.check_budget()

    def count_llm(self, cost: float) -> None:
        self.data["counters"]["llm_calls"] += 1
        self.data["counters"]["llm_cost_usd"] = round(self.data["counters"]["llm_cost_usd"] + cost, 4)
        self.check_budget()

    def check_budget(self, budget: dict | None = None) -> None:
        b = budget or self.data.get("budget") or {}
        c = self.data["counters"]
        elapsed_min = (c.get("elapsed_s_prev", 0.0) + time.monotonic() - self._t0) / 60
        problems = []
        if b.get("max_fetches") and c["fetches"] > b["max_fetches"]:
            problems.append(f"fetches {c['fetches']} > {b['max_fetches']}")
        if b.get("max_minutes") and elapsed_min > b["max_minutes"]:
            problems.append(f"elapsed {elapsed_min:.0f} min > {b['max_minutes']}")
        if b.get("max_llm_calls") and c["llm_calls"] > b["max_llm_calls"]:
            problems.append(f"model calls {c['llm_calls']} > {b['max_llm_calls']}")
        if b.get("max_llm_cost_usd") and c["llm_cost_usd"] > b["max_llm_cost_usd"]:
            problems.append(f"model cost ${c['llm_cost_usd']} > ${b['max_llm_cost_usd']}")
        if problems:
            self.save()
            raise Paused(BUDGET_STOP, "Budget exhausted: " + "; ".join(problems))

    # ---------------------------------------------------------------- artifacts
    def put(self, name: str, obj: Any) -> None:
        write_json(self.work / f"{name}.json", obj)

    def get(self, name: str, default: Any = None) -> Any:
        return read_json(self.work / f"{name}.json", default)
