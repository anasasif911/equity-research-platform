"""A single model definition evaluated two ways.

Every forecast/valuation line is defined ONCE as a formula template in a tiny
language. The Python engine evaluates the template to produce the numbers
used in reports; the Excel writer translates the same template to native
Excel formulas. An independent engine (LibreOffice) then recalculates the
workbook and the results are compared cell by cell (gate G12). This removes
the classic failure mode where the spreadsheet and the report disagree.

Template tokens
  {r:key}            same sheet, same column
  {r:key@-1}         same sheet, previous column
  {r:Sheet.key}      other sheet (sheets share the year-column layout)
  {r:Sheet.key@-1}
  {s:name}           named scalar cell
  {range:Sheet.key}  the forecast-column range of a row (for SUM)
  {last:Sheet.key}   the last forecast column of a row
  {first:Sheet.key}  the first forecast column of a row
  {hl:Sheet.key}     the last historical column of a row
  {f3:Sheet.key}     forecast column 3 (0-based) of a row
Operators: + - * / ^ ( ) and functions MAX MIN ABS SUM CHOOSE.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from openpyxl.utils import get_column_letter

TOKEN = re.compile(r"\{(r|s|range|last|first|hl|f\d+):(?:([A-Za-z_][A-Za-z0-9_]*)\.)?([A-Za-z0-9_]+)(@-1)?\}")
FIRST_DATA_COL = 3  # A=label, B=unit/notes, C.. = periods
FIRST_ROW = 7


@dataclass
class Row:
    sheet: str
    key: str
    label: str
    unit: str = ""
    hist: Any = None          # list of values, a template applied to every hist column, or None
    fc: Any = None            # template string, or list of values (inputs)
    fmt: str = "millions"
    style: str = "normal"     # normal | total | memo | input | header | check
    note: str = ""
    source: str = ""          # evidence ids / description for hist values


@dataclass
class Scalar:
    sheet: str
    name: str
    label: str
    value: Any                # number (input) or template string
    fmt: str = "millions"
    note: str = ""
    source: str = ""
    style: str = "normal"     # input | normal | total | check
    cell: str | None = None   # explicit placement, e.g. "D9" (grids); default column C of a block


@dataclass
class ModelSpec:
    hist_labels: list[str]
    fc_labels: list[str]
    sheets: list[str]
    rows: list[Row] = field(default_factory=list)
    scalars: list[Scalar] = field(default_factory=list)
    sheet_titles: dict[str, str] = field(default_factory=dict)
    # filled by layout()
    row_pos: dict[tuple[str, str], int] = field(default_factory=dict)
    scalar_pos: dict[str, tuple[str, str]] = field(default_factory=dict)   # name -> (sheet, "C12")

    @property
    def H(self) -> int:
        return len(self.hist_labels)

    @property
    def N(self) -> int:
        return len(self.fc_labels)

    def add(self, *rows: Row):
        self.rows.extend(rows)

    def scalar(self, *sc: Scalar):
        self.scalars.extend(sc)

    def layout(self) -> None:
        """Assign Excel rows: per sheet, period rows first then a scalar block."""
        nxt = {s: FIRST_ROW for s in self.sheets}
        for r in self.rows:
            if r.style == "header":
                nxt[r.sheet] += 1 if nxt[r.sheet] > FIRST_ROW else 0
            self.row_pos[(r.sheet, r.key)] = nxt[r.sheet]
            nxt[r.sheet] += 1
        for s in self.scalars:
            if s.cell:
                self.scalar_pos[s.name] = (s.sheet, s.cell)
                continue
            if s.sheet not in self._scalar_started:
                self._scalar_started.add(s.sheet)
                nxt[s.sheet] += 2 if nxt[s.sheet] > FIRST_ROW else 0
            self.scalar_pos[s.name] = (s.sheet, f"C{nxt[s.sheet]}")
            nxt[s.sheet] += 1

    _scalar_started: set = field(default_factory=set)

    def col_letter(self, c: int) -> str:
        return get_column_letter(FIRST_DATA_COL + c)

    # ------------------------------------------------------------------ Excel
    def to_excel(self, template: str, sheet: str, c: int | None) -> str:
        def rep(m: re.Match) -> str:
            kind, sh, key, prev = m.group(1), m.group(2), m.group(3), m.group(4)
            if kind == "s":
                ssheet, cell = self.scalar_pos[key]
                m2 = re.match(r"([A-Z]+)(\d+)", cell)
                absref = f"${m2.group(1)}${m2.group(2)}"
                return f"{_q(ssheet)}!{absref}" if ssheet != sheet else absref
            sh = sh or sheet
            row = self.row_pos[(sh, key)]
            pre = f"{_q(sh)}!" if sh != sheet else ""
            if kind == "range":
                return f"{pre}{self.col_letter(self.H)}{row}:{self.col_letter(self.H + self.N - 1)}{row}"
            if kind == "last":
                return f"{pre}${self.col_letter(self.H + self.N - 1)}${row}"
            if kind == "first":
                return f"{pre}${self.col_letter(self.H)}${row}"
            if kind == "hl":
                return f"{pre}${self.col_letter(self.H - 1)}${row}"
            if kind.startswith("f"):
                return f"{pre}${self.col_letter(self.H + int(kind[1:]))}${row}"
            cc = c - (1 if prev else 0)
            return f"{pre}{self.col_letter(cc)}{row}"
        return "=" + TOKEN.sub(rep, template)


def _q(sheet: str) -> str:
    return f"'{sheet}'" if not sheet.isalnum() else sheet


class EvalError(Exception):
    pass


def evaluate(spec: ModelSpec, scenario_inputs: dict | None = None) -> dict:
    """Evaluate the spec. Returns {'rows': {(sheet,key): [values]}, 'scalars': {name: value}}.
    Excel semantics mirrored: blank cells are 0 in arithmetic; errors become None."""
    ncol = spec.H + spec.N
    rv: dict[tuple[str, str], list] = {}
    sv: dict[str, Any] = {}
    errors: list[str] = []

    has_src = {(r.sheet, r.key): (r.hist is not None, r.fc is not None) for r in spec.rows}
    computed: set = set()

    def r_(sh, key, c):
        if c < 0:
            return 0.0
        vals = rv.get((sh, key))
        if vals is None:
            raise EvalError(f"unknown row {sh}.{key}")
        src = has_src[(sh, key)][1 if c >= spec.H else 0]
        if src and (sh, key, c) not in computed:
            raise EvalError(f"row-order dependency: {sh}.{key}[{c}] used before it is computed")
        v = vals[c]
        return 0.0 if v is None else v

    def s_(name):
        if name not in sv:
            raise EvalError(f"scalar {name} referenced before definition")
        v = sv[name]
        return 0.0 if v is None else v

    def rng_(sh, key):
        return [0.0 if v is None else v for v in rv[(sh, key)][spec.H:]]

    env = {"MAX": max, "MIN": min, "ABS": abs, "CHOOSE": lambda i, *a: a[int(i) - 1], "SUM": lambda *a: sum(sum(x) if isinstance(x, list) else x for x in a),
           "_r": r_, "_s": s_, "_rng": rng_}

    def compile_py(template: str, sheet: str) -> str:
        def rep(m):
            kind, sh, key, prev = m.group(1), m.group(2) or sheet, m.group(3), m.group(4)
            if kind == "s":
                return f"_s('{key}')"
            if kind == "range":
                return f"_rng('{sh}','{key}')"
            if kind == "last":
                return f"_r('{sh}','{key}',{spec.H + spec.N - 1})"
            if kind == "first":
                return f"_r('{sh}','{key}',{spec.H})"
            if kind == "hl":
                return f"_r('{sh}','{key}',{spec.H - 1})"
            if kind.startswith("f"):
                return f"_r('{sh}','{key}',{spec.H + int(kind[1:])})"
            return f"_r('{sh}','{key}',c{'-1' if prev else ''})"
        return TOKEN.sub(rep, template).replace("^", "**")

    def run(expr: str, c: int | None, where: str):
        try:
            v = eval(expr, env, {"c": c})
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
        except ZeroDivisionError:
            errors.append(f"#DIV/0! at {where}")
            return None
        except EvalError:
            raise
        except (TypeError, ValueError, OverflowError) as e:
            errors.append(f"error at {where}: {e}")
            return None

    # scalars that do not depend on rows are evaluated first, the rest after rows
    pending_scalars = list(spec.scalars)

    def eval_scalars(only_independent: bool):
        nonlocal pending_scalars
        rest = []
        for s in pending_scalars:
            if isinstance(s.value, str):
                deps_rows = re.search(r"\{(r|range|last|first|hl|f\d+):", s.value)
                if only_independent and deps_rows:
                    rest.append(s)
                    continue
                if only_independent and any(n not in sv for n in re.findall(r"\{s:([A-Za-z0-9_]+)\}", s.value)):
                    rest.append(s)
                    continue
                sv[s.name] = run(compile_py(s.value, s.sheet), None, f"{s.sheet}!{s.name}")
            else:
                sv[s.name] = s.value
        pending_scalars = rest

    eval_scalars(True)
    for r in spec.rows:
        vals: list = [None] * ncol
        rv[(r.sheet, r.key)] = vals
    # evaluate column by column so prev-column references resolve; rows in order within a column
    for c in range(ncol):
        is_fc = c >= spec.H
        for r in spec.rows:
            src = r.fc if is_fc else r.hist
            if src is None:
                continue
            if isinstance(src, list):
                vals = src[c - spec.H] if is_fc else src[c]
                rv[(r.sheet, r.key)][c] = vals
                computed.add((r.sheet, r.key, c))
            else:
                rv[(r.sheet, r.key)][c] = run(compile_py(src, r.sheet), c, f"{r.sheet}.{r.key}[{c}]")
            computed.add((r.sheet, r.key, c))
        # scalars depending only on already-complete rows are evaluated at the end
    eval_scalars(False)
    return {"rows": rv, "scalars": sv, "errors": errors}
