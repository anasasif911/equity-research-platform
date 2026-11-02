"""Configurable inference-model interface for judgment-heavy tasks.

Storing the methodology outside the model (methodology/*.yaml, prompts/*.md)
makes runs reproducible and auditable, but it does NOT remove the need for an
inference model: evidence synthesis, thesis development, counter-thesis and
narrative critique still require one. Providers:

  anthropic      Claude via the official Anthropic SDK (needs ANTHROPIC_API_KEY
                 or another SDK credential source)
  agent_handoff  writes work/model_requests/<task>.json; any external agent
                 (e.g. a Claude Code session) answers in
                 work/model_responses/<task>.json; the run pauses and resumes
  none           no model; qualitative sections are explicitly marked as not
                 synthesised and gate G19 is NOT_RUN
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .job import AWAITING_MODEL, Paused
from .util import log, read_json, utcnow, write_json


class ModelProvider:
    name = "base"

    def complete(self, task: str, system: str, prompt: str, schema: dict) -> dict | None:
        raise NotImplementedError


class NoModel(ModelProvider):
    name = "none"

    def complete(self, task, system, prompt, schema):
        return None


def validate_schema(obj: Any, schema: dict, path: str = "$") -> list[str]:
    """Small JSON-schema subset validator (type/required/enum/items/properties)."""
    errs: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float), "integer": int}
    if t and not isinstance(obj, types[t]):
        return [f"{path}: expected {t}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if t == "object":
        for k in schema.get("required", []):
            if k not in obj:
                errs.append(f"{path}.{k}: missing")
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                errs.extend(validate_schema(obj[k], sub, f"{path}.{k}"))
    if t == "array":
        for i, it in enumerate(obj):
            errs.extend(validate_schema(it, schema.get("items", {}), f"{path}[{i}]"))
    return errs


class AgentHandoff(ModelProvider):
    name = "agent_handoff"

    def __init__(self, work: Path):
        self.req_dir = work / "model_requests"
        self.resp_dir = work / "model_responses"

    def complete(self, task, system, prompt, schema):
        resp_p = self.resp_dir / f"{task}.json"
        if resp_p.exists():
            obj = read_json(resp_p)
            errs = validate_schema(obj, schema)
            if errs:
                raise Paused(AWAITING_MODEL, f"Response for '{task}' fails schema: {errs[:5]}",
                             {"request": str(self.req_dir / f'{task}.json')})
            return obj
        write_json(self.req_dir / f"{task}.json", {
            "task": task, "created_at": utcnow().isoformat(), "system": system, "prompt": prompt, "schema": schema,
            "instructions": f"Answer with JSON matching 'schema' and save it to {resp_p}. Then run: eqr resume <run_dir>",
        })
        raise Paused(AWAITING_MODEL, f"Model task '{task}' handed off: answer {self.req_dir / (task + '.json')}",
                     {"request": str(self.req_dir / f"{task}.json"), "response_path": str(resp_p)})


class AnthropicModel(ModelProvider):
    name = "anthropic"

    def __init__(self, cfg: dict, state=None):
        import anthropic  # official SDK
        self.client = anthropic.Anthropic()
        self.cfg = cfg["model"]
        self.state = state

    def complete(self, task, system, prompt, schema):
        with self.client.beta.messages.stream(
            model=self.cfg["anthropic_model"],
            max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"],
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.get("effort", "high"),
                           "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"fallbacks": "default"},
        ) as stream:
            msg = stream.get_final_message()
        u = msg.usage
        cost = (u.input_tokens * self.cfg["price_per_mtok_in"] + u.output_tokens * self.cfg["price_per_mtok_out"]) / 1e6
        if self.state is not None:
            self.state.count_llm(cost)
        if msg.stop_reason == "refusal":
            log.warning("model declined task %s", task)
            return None
        if msg.stop_reason == "max_tokens":
            log.warning("model output truncated for task %s", task)
            return None
        text = next((b.text for b in msg.content if b.type == "text"), None)
        if not text:
            return None
        obj = json.loads(text)
        errs = validate_schema(obj, schema)
        if errs:
            log.warning("model output schema issues for %s: %s", task, errs[:5])
        return obj


def make_provider(cfg: dict, work: Path, state=None) -> ModelProvider:
    p = cfg["model"]["provider"]
    if p == "auto":
        p = "anthropic" if (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")) else "agent_handoff"
    if p == "anthropic":
        return AnthropicModel(cfg, state)
    if p == "agent_handoff":
        return AgentHandoff(work)
    return NoModel()
