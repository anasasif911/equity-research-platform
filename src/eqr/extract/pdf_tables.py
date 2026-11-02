"""Financial statements from PDF reports (PSX and other non-XBRL markets).

Column-aware parsing from word positions (pdfplumber), tolerant of OCR
artefacts in scanned reports:
  * thousands separators read as '.' (e.g. 81,654.298,337) - rupee figures are
    whole numbers, so all separators are dropped;
  * '.', '-', '■' in an amount cell mean nil;
  * labels wrapped onto two lines around their numbers.
Every value keeps (document evidence id, page, raw label). Nothing is
trusted until the accounting identities and cross-document checks in
`reconcile_sources` have run."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pdfplumber

YEAR = re.compile(r"^(19|20)\d\d$")
AMOUNT = re.compile(r"^\(?-?\d{1,3}([,\.]\d{3})+\)?$|^\(?\d{4,}\)?$")
NIL = {"-", ".", "–", "—", "■", "-   ", "--"}
PER_SHARE = re.compile(r"^\(?\d{1,4}\.\d{2}\)?$")


SECTION_PREFIX = re.compile(r"^(equity and liabilities|share capital and reserves|non[- ]current liabilities|current liabilities|"
                            r"non[- ]current assets|current assets|assets|liabilities)\s+(?=\S)")


def norm_label(s: str) -> str:
    s = s.lower().replace("’", "'").replace("–", "-")
    s = re.sub(r"\s+", " ", s).strip(" :|")
    prev = None
    while prev != s:          # a section heading printed on the line above can merge into the first row label
        prev = s
        s = SECTION_PREFIX.sub("", s)
    return s


def parse_amount(tok: str, per_share: bool = False) -> float | None:
    t = tok.strip().strip("|")
    if t in NIL:
        return 0.0
    neg = t.startswith("(") or t.endswith(")") or t.startswith("-")
    t = t.strip("()-")
    if per_share and PER_SHARE.match(t):
        v = float(t)
    else:
        if not re.fullmatch(r"\d[\d,\.]*", t):
            return None
        v = float(re.sub(r"[,\.]", "", t))
    return -v if neg else v


@dataclass
class TableRow:
    label: str
    values: list
    top: float
    raw_tokens: list = field(default_factory=list)


@dataclass
class Table:
    page: int
    columns: list           # header tokens per column (e.g. years)
    rows: list
    heading: str


def _lines(words, tol=2.5):
    out: list[list] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if out and abs(out[-1][0]["top"] - w["top"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def parse_page(pdf, page_index: int, min_cols: int = 2) -> Table | None:
    """Parse one statement page into rows of (label, [values per column])."""
    pg = pdf.pages[page_index]
    words = pg.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return None
    # header: first line with >= min_cols year tokens to the right of the label area
    header_line = None
    for ln in _lines(words):
        yrs = [w for w in ln if YEAR.match(w["text"].strip("|,")) and w["x0"] > pg.width * 0.2]
        if len(yrs) >= min_cols:
            header_line = yrs
            break
    if not header_line:
        return None
    cols = []
    for w in sorted(header_line, key=lambda w: w["x0"]):
        c = (w["x0"] + w["x1"]) / 2
        if not cols or c - cols[-1]["c"] > 25:
            cols.append({"c": c, "label": w["text"].strip("|,"), "x1": w["x1"]})
    seen: dict = {}
    for c in cols:   # duplicate headers (e.g. "2026" for 30 Sep and 30 Jun) -> positional suffix
        n = seen.get(c["label"], 0)
        seen[c["label"]] = n + 1
        if n:
            c["label"] = f"{c['label']}#{n + 1}"
    hdr_top = header_line[0]["top"]
    spacing = min((b["c"] - a["c"] for a, b in zip(cols, cols[1:])), default=120)
    left_edge = cols[0]["c"] - max(spacing * 0.75, 55)
    heading = " ".join(w["text"] for w in words if w["top"] < hdr_top)[:200]
    amt_tokens, label_words = [], []
    for w in words:
        if w["top"] <= hdr_top + 3:
            continue
        t = w["text"]
        cx = (w["x0"] + w["x1"]) / 2
        if cx >= left_edge and (AMOUNT.match(t.strip("|")) or t.strip("|") in NIL or PER_SHARE.match(t.strip("()|"))):
            j = min(range(len(cols)), key=lambda k: abs(cols[k]["c"] - cx))
            if abs(cols[j]["c"] - cx) <= max(spacing * 0.6, 40):
                amt_tokens.append((w, j))
                continue
        if w["x1"] < left_edge + 10:
            label_words.append(w)
    # group amounts into rows
    amt_rows: list[dict] = []
    for w, j in sorted(amt_tokens, key=lambda x: x[0]["top"]):
        if amt_rows and abs(amt_rows[-1]["top"] - w["top"]) <= 3.5:
            amt_rows[-1]["cells"].setdefault(j, w["text"])
        else:
            amt_rows.append({"top": w["top"], "cells": {j: w["text"]}, "labels": []})
    if not amt_rows:
        return None
    # assign each label line to the nearest amount row (within 11pt)
    for ln in _lines(label_words):
        y = ln[0]["top"]
        txt = " ".join(w["text"] for w in ln if not re.fullmatch(r"\d{1,2}(\.\d{1,2}){0,2}", w["text"]))
        best = min(amt_rows, key=lambda r: abs(r["top"] - y))
        if abs(best["top"] - y) <= 11:
            best["labels"].append((y, txt))
    # merge OCR-split rows: complementary columns within 12pt, one without a label
    merged = []
    for r in amt_rows:
        if merged and r["top"] - merged[-1]["top"] <= 12 and not (set(r["cells"]) & set(merged[-1]["cells"])) \
                and (not r["labels"] or not merged[-1]["labels"]):
            merged[-1]["cells"].update(r["cells"])
            merged[-1]["labels"] += r["labels"]
            continue
        merged.append(r)
    rows = []
    for r in merged:
        label = " ".join(t for _, t in sorted(r["labels"])).strip()
        ps = bool(re.search(r"per share|eps", label, re.I))
        vals = [parse_amount(r["cells"][k], per_share=ps) if k in r["cells"] else None for k in range(len(cols))]
        rows.append(TableRow(label=label, values=vals, top=r["top"], raw_tokens=[r["cells"].get(k) for k in range(len(cols))]))
    return Table(page=page_index + 1, columns=[c["label"] for c in cols], rows=rows, heading=heading)


def find_pages(pdf, must: list[str], any_of: list[str] | None = None, exclude: list[str] | None = None,
               start: int = 0) -> list[int]:
    out = []
    for i in range(start, len(pdf.pages)):
        t = re.sub(r"\s+", " ", (pdf.pages[i].extract_text() or "").upper())
        if all(m.upper() in t for m in must) and (not any_of or any(a.upper() in t for a in any_of)) \
                and not any(e.upper() in t for e in (exclude or [])):
            out.append(i)
    return out


def map_rows(table: Table, rules: dict) -> dict:
    """{canonical_item: {column_label: (value, raw_label, raw_token)}}; first rule match wins; duplicates summed only
    for items listed in rules['_add']"""
    out: dict = {}
    compiled = [(item, [re.compile(p) for p in pats]) for item, pats in rules.items() if not item.startswith("_")]
    for r in table.rows:
        lab = norm_label(r.label)
        if not lab:
            continue
        for item, pats in compiled:
            if any(p.search(lab) for p in pats):
                if item in out and item not in rules.get("_add", []):
                    break
                for c, v in zip(table.columns, r.values):
                    if v is None:
                        continue
                    if item in out and c in out[item]:
                        prev = out[item][c]
                        out[item][c] = (prev[0] + v, prev[1] + " + " + r.label, prev[2])
                    else:
                        out.setdefault(item, {})[c] = (v, r.label, r.raw_tokens[table.columns.index(c)])
                break
    return out


def open_pdf(path):
    return pdfplumber.open(path)
