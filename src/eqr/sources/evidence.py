"""Evidence store, retrieval with caching/rate limiting/retries, and the
evidence inbox.

Every byte the pipeline reasons over is registered here with provenance
(URL, publisher, tier, retrieval time, sha256, retrieval method). When a
host is unreachable from the execution environment (network policy, outage)
the request is recorded in retrieval_requests.json with a deterministic
inbox filename. Any agent or human with access may place the exact source
document there (plus a .meta.json describing how it was obtained); the run
then resumes and treats it like any other source - it is still parsed and
validated by code, never trusted blindly.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import requests

from ..config import user_agent
from ..util import log, read_json, sha256_bytes, utcnow, write_json


class SourceUnavailable(Exception):
    def __init__(self, url: str, reason: str, request_id: str):
        super().__init__(f"{url}: {reason}")
        self.url, self.reason, self.request_id = url, reason, request_id


class BudgetExceeded(Exception):
    pass


@dataclass
class Evidence:
    id: str
    kind: str
    title: str
    publisher: str
    url: str | None
    tier: int
    retrieved_at: str
    method: str                       # http | cache | inbox | fixture | derived
    sha256: str | None = None
    local_path: str | None = None
    published_at: str | None = None
    period: str | None = None
    notes: str | None = None
    synthetic: bool = False
    extra: dict = field(default_factory=dict)


def request_id_for(kind: str, url: str) -> str:
    return f"{kind}-{hashlib.sha1(url.encode()).hexdigest()[:10]}"


class _RateLimiter:
    def __init__(self, per_second: dict):
        self.per_second = per_second
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def wait(self, host: str) -> None:
        rate = self.per_second.get(host, self.per_second.get("default", 2))
        with self.lock:
            now = time.monotonic()
            gap = 1.0 / rate
            t = self.last.get(host, 0.0)
            if now - t < gap:
                time.sleep(gap - (now - t))
            self.last[host] = time.monotonic()


class EvidenceStore:
    def __init__(self, run_dir: Path, cfg: dict, policy: dict, ticker: str, state=None):
        self.run_dir = run_dir
        self.cfg = cfg
        self.policy = policy
        self.ticker = ticker.upper()
        self.state = state
        self.raw_dir = run_dir / "sources" / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = Path(cfg["cache_dir"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dirs = [Path(cfg["inbox_dir"]) / self.ticker, Path(cfg["inbox_dir"]) / "_shared",
                           run_dir / "inbox"]
        self._path = run_dir / "work" / "evidence.json"
        self._req_path = run_dir / "work" / "retrieval_requests.json"
        self.items: dict[str, Evidence] = {e["id"]: Evidence(**e) for e in read_json(self._path, [])}
        self.requests: dict[str, dict] = {r["id"]: r for r in read_json(self._req_path, [])}
        self.limiter = _RateLimiter(policy.get("rate_limits_per_second", {}))
        self.fixtures: list[tuple[str, Path]] = []   # (url regex, file) - offline SYNTHETIC test mode only
        self.session = requests.Session()
        # The SEC contact user-agent (contains an email) is sent ONLY to SEC hosts;
        # every other host receives a generic identifier.
        self.sec_user_agent = user_agent(cfg)
        self.generic_user_agent = cfg["http"].get("generic_user_agent", "eqr-research-platform/0.1")
        self.session.headers.update({"Accept-Encoding": "gzip, deflate"})

    # ------------------------------------------------------------------ registry
    def save(self) -> None:
        write_json(self._path, [asdict(e) for e in self.items.values()])
        write_json(self._req_path, list(self.requests.values()))

    def add(self, **kw) -> Evidence:
        # identical source (same kind+url+sha) keeps one id: idempotent on resume
        for e in self.items.values():
            if e.kind == kw.get("kind") and e.url == kw.get("url") and e.sha256 == kw.get("sha256") and e.title == kw.get("title"):
                return e
        eid = f"EV-{len(self.items) + 1:04d}"
        ev = Evidence(id=eid, **kw)
        self.items[eid] = ev
        self.save()
        return ev

    def get(self, eid: str) -> Evidence:
        return self.items[eid]

    # ------------------------------------------------------------------ retrieval
    def _inbox_lookup(self, rid: str) -> tuple[bytes, dict] | None:
        for d in self.inbox_dirs:
            if not d.exists():
                continue
            for p in d.glob(f"{rid}.*"):
                if p.name.endswith(".meta.json"):
                    continue
                meta = read_json(p.with_name(f"{rid}.meta.json"), {})
                return p.read_bytes(), meta
        return None

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        h = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.cache_dir / f"{h}.bin", self.cache_dir / f"{h}.meta.json"

    def fetch(self, url: str, *, kind: str, title: str, publisher: str, tier: int,
              ttl_hours: float = 24, critical: bool = False, purpose: str = "",
              accept: str | None = None) -> tuple[bytes, Evidence]:
        rid = request_id_for(kind, url)
        ext = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
        req = self.requests.setdefault(rid, {
            "id": rid, "kind": kind, "url": url, "title": title, "publisher": publisher,
            "purpose": purpose, "critical": critical, "status": "pending", "attempts": 0,
            "inbox_filename": f"{rid}{ext if len(ext) <= 6 else '.bin'}", "last_error": None,
        })
        # 0. offline fixture mode (tests): labelled synthetic, never mixed with live runs
        for pat, path in self.fixtures:
            if re.search(pat, url):
                req["status"] = "fulfilled_fixture"
                data, ev = self.register_fixture(path, url=url, kind=kind, title=f"[SYNTHETIC FIXTURE] {title}",
                                                 publisher=f"{publisher} (synthetic test fixture)", tier=tier,
                                                 notes="Fictional data for tests; not a real source.")
                self.save()
                return data, ev
        # 1. evidence inbox (explicitly supplied source snapshot)
        hit = self._inbox_lookup(rid)
        if hit:
            data, meta = hit
            req["status"] = "fulfilled_inbox"
            ev = self._register(data, url=meta.get("url", url), kind=kind, title=title, publisher=publisher,
                                tier=tier, method="inbox", retrieved_at=meta.get("retrieved_at") or utcnow().isoformat(),
                                notes=f"Supplied via evidence inbox; retrieval method: {meta.get('method', 'unspecified')}; "
                                      f"retrieved by: {meta.get('retrieved_by', 'unspecified')}. {meta.get('notes', '')}".strip(),
                                ext=ext)
            self.save()
            return data, ev
        # 2. fresh cache
        bin_p, meta_p = self._cache_paths(url)
        if bin_p.exists() and meta_p.exists():
            meta = read_json(meta_p)
            age_h = (utcnow().timestamp() - meta["fetched_epoch"]) / 3600
            if age_h <= ttl_hours:
                data = bin_p.read_bytes()
                req["status"] = "fulfilled_cache"
                ev = self._register(data, url=url, kind=kind, title=title, publisher=publisher, tier=tier,
                                    method="http(cache)", retrieved_at=meta["retrieved_at"], ext=ext)
                self.save()
                return data, ev
        # 3. network with retries and backoff
        if self.state is not None:
            self.state.count_fetch()
        host = urllib.parse.urlparse(url).netloc
        backoff = self.cfg["http"]["retry"]["backoff_s"]
        max_attempts = self.cfg["http"]["retry"]["max_attempts"]
        last_err = None
        for attempt in range(max_attempts + 2):  # two extra attempts reserved for 429 back-off
            self.limiter.wait(host)
            req["attempts"] += 1
            try:
                headers = {"Accept": accept} if accept else {}
                headers["User-Agent"] = self.sec_user_agent if host.endswith("sec.gov") else self.generic_user_agent
                r = self.session.get(url, timeout=self.cfg["http"]["timeout_s"], headers=headers)
                if r.status_code == 200:
                    data = r.content
                    bin_p.write_bytes(data)
                    write_json(meta_p, {"url": url, "retrieved_at": utcnow().isoformat(),
                                        "fetched_epoch": utcnow().timestamp(), "status": 200})
                    req["status"] = "fulfilled_http"
                    ev = self._register(data, url=url, kind=kind, title=title, publisher=publisher, tier=tier,
                                        method="http", retrieved_at=utcnow().isoformat(), ext=ext)
                    self.save()
                    return data, ev
                last_err = f"HTTP {r.status_code}"
                if r.status_code in (403, 404, 401, 451):
                    break  # policy / not found: retrying will not help
                if r.status_code == 429:  # honour Retry-After; otherwise back off progressively
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if (ra and ra.isdigit()) else 15.0 * (attempt + 1)
                    time.sleep(min(wait, 90.0))
                    continue
            except requests.exceptions.ProxyError as e:
                last_err = f"blocked by network policy/proxy: {str(e)[:160]}"
                break
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {str(e)[:160]}"
            if attempt >= max_attempts - 1 and not (last_err or "").endswith("429"):
                break
            if attempt < max_attempts - 1:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
        req["status"] = "blocked"
        req["last_error"] = last_err
        self.save()
        log.warning("retrieval failed (%s): %s -> place source at %s/%s", last_err, url,
                    self.inbox_dirs[0], req["inbox_filename"])
        raise SourceUnavailable(url, last_err or "unknown", rid)

    def _register(self, data: bytes, *, ext: str, **kw) -> Evidence:
        sha = sha256_bytes(data)
        local = self.raw_dir / f"{kw['kind']}-{sha[:12]}{ext if len(ext) <= 6 else '.bin'}"
        if not local.exists():
            local.write_bytes(data)
        return self.add(sha256=sha, local_path=str(local.relative_to(self.run_dir)), **kw)

    def register_fixture(self, path: Path, **kw) -> tuple[bytes, Evidence]:
        data = path.read_bytes()
        ev = self._register(data, ext=path.suffix, method="fixture", synthetic=True,
                            retrieved_at=utcnow().isoformat(), **kw)
        return data, ev

    def mark_invalid(self, url: str, kind: str, reason: str) -> None:
        """A 200 response whose content failed validation (e.g. a bot-check page)."""
        rid = request_id_for(kind, url)
        if rid in self.requests:
            self.requests[rid].update(status="invalid_content", last_error=reason)
        for e in self.items.values():
            if e.url == url and e.kind == kind:
                e.notes = f"REJECTED: {reason}"
        self.save()

    def blocked_requests(self, critical_only: bool = False) -> list[dict]:
        return [r for r in self.requests.values() if r["status"] == "blocked" and (r["critical"] or not critical_only)]

    def source_register(self) -> list[dict]:
        rows = []
        for e in self.items.values():
            rows.append({"id": e.id, "title": e.title, "publisher": e.publisher, "tier": e.tier,
                         "url": e.url, "kind": e.kind, "period": e.period, "published_at": e.published_at,
                         "retrieved_at": e.retrieved_at, "method": e.method, "sha256": e.sha256,
                         "local_path": e.local_path, "synthetic": e.synthetic, "notes": e.notes})
        return rows
