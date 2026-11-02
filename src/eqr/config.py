"""Configuration and methodology loading. Methodology lives in version-controlled
YAML under methodology/; code reads it, it is never embedded in prompts or code."""
from __future__ import annotations

import copy
import functools
import os
from pathlib import Path
from typing import Any

import yaml

from .util import ROOT

METHODOLOGY_DIR = ROOT / "methodology"
BRAND_FILE = ROOT / "brand" / "tokens.yaml"


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    cfg = load_yaml(ROOT / "config" / "default.yaml")
    if path:
        cfg = deep_merge(cfg, load_yaml(Path(path)))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    for key in ("runs_dir", "inbox_dir", "cache_dir"):
        p = Path(cfg[key])
        cfg[key] = str(p if p.is_absolute() else ROOT / p)
    return cfg


def user_agent(cfg: dict) -> str:
    return os.environ.get(cfg["http"]["user_agent_env"]) or cfg["http"]["default_user_agent"]


class Methodology:
    """Typed access to the methodology assets."""

    def __init__(self, root: Path = METHODOLOGY_DIR):
        self.root = root

    @functools.cached_property
    def source_policy(self) -> dict:
        return load_yaml(self.root / "source_policy.yaml")

    @functools.cached_property
    def normalization(self) -> dict:
        return load_yaml(self.root / "normalization_rules.yaml")

    @functools.cached_property
    def valuation(self) -> dict:
        return load_yaml(self.root / "valuation_selection.yaml")

    @functools.cached_property
    def forecasting(self) -> dict:
        return load_yaml(self.root / "forecasting.yaml")

    @functools.cached_property
    def cost_of_capital(self) -> dict:
        return load_yaml(self.root / "cost_of_capital.yaml")

    @functools.cached_property
    def gates(self) -> dict:
        return load_yaml(self.root / "quality_gates.yaml")

    @functools.cached_property
    def research_plan(self) -> dict:
        return load_yaml(self.root / "research_plan.yaml")

    @functools.cached_property
    def critique(self) -> dict:
        return load_yaml(self.root / "critique_checklists.yaml")

    def prompt(self, name: str) -> str:
        return (self.root / "prompts" / f"{name}.md").read_text()

    def playbook_for_sic(self, sic: int | None, override: str | None = None) -> dict:
        idx = load_yaml(self.root / "playbooks" / "sector_index.yaml")
        name = override
        if not name and sic is not None:
            for m in idx["mapping"]:
                lo, hi = m["sic"]
                if lo <= sic <= hi:
                    name = m["playbook"]
                    break
        name = name or idx["default"]
        pb = load_yaml(self.root / "playbooks" / "sectors" / f"{name}.yaml")
        general = load_yaml(self.root / "playbooks" / "sectors" / "general.yaml")
        # Sector exposures extend general ones (macro rates/FX apply to all)
        if name != "general":
            seen = {e["factor"] for e in pb.get("exposures", [])}
            pb["exposures"] = pb.get("exposures", []) + [e for e in general["exposures"] if e["factor"] not in seen]
        return pb

    def version_fingerprint(self) -> dict:
        import hashlib
        h = hashlib.sha256()
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                h.update(p.name.encode()); h.update(p.read_bytes())
        return {"methodology_sha256": h.hexdigest()[:16]}


def brand() -> dict:
    return load_yaml(BRAND_FILE)
