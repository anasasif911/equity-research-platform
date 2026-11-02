from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

log = logging.getLogger("eqr")


def setup_logging(verbose: bool = False, logfile: Path | None = None) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt,
                        handlers=handlers, force=True)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(ts: dt.datetime | dt.date | None) -> str | None:
    return ts.isoformat() if ts else None


def parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    return dt.date.fromisoformat(str(s)[:10])


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _Enc(json.JSONEncoder):
    def default(self, o: Any):
        if isinstance(o, (dt.date, dt.datetime)):
            return o.isoformat()
        if isinstance(o, Path):
            return str(o)
        if hasattr(o, "__dict__"):
            return o.__dict__
        if hasattr(o, "item"):  # numpy scalars
            return o.item()
        return super().default(o)


def _clean(o: Any) -> Any:
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_clean(obj), indent=2, cls=_Enc, ensure_ascii=False))
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written checkpoint


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def cagr(first: float | None, last: float | None, years: float) -> float | None:
    if not first or not last or years <= 0 or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def fmt_money(v: float | None, dp: int = 0) -> str:
    if v is None:
        return "n/a"
    s = f"{abs(v):,.{dp}f}"
    return f"({s})" if v < 0 else s


def fmt_pct(v: float | None, dp: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:.{dp}f}%"
