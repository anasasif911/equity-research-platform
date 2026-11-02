"""Report charts (PNG, 200 dpi) in the house palette: ink for history, gold
for the focal/forecast series, greys for context. Every chart carries units
and a source note in the report caption."""
from __future__ import annotations

from pathlib import Path

from .theme import c, mpl_setup


def _save(fig, path: Path):
    fig.savefig(path, facecolor="white")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return path


def revenue_margin(path: Path, labels, revenue, margin, n_hist: int, currency: str):
    plt = mpl_setup()
    fig, ax = plt.subplots(figsize=(6.6, 2.6))
    colors = [c("ink")] * n_hist + [c("gold")] * (len(labels) - n_hist)
    short = [l.replace("FY20", "FY") for l in labels]
    ax.bar(short, [r / 1e3 for r in revenue], color=colors, width=0.62)
    ax.set_ylabel(f"Revenue ({currency} bn)")
    ax.tick_params(axis="x", labelrotation=0, labelsize=6.5)
    ax.set_ylim(0, max(r / 1e3 for r in revenue) * 1.15)
    ax2 = ax.twinx()
    mp = [m * 100 for m in margin]
    ax2.plot(short, mp, color=c("grey_600"), marker="o", ms=3, lw=1.2)
    lo, hi = min(min(mp), 0), max(mp)
    ax2.set_ylim(lo * 1.2 if lo < 0 else 0, hi * 1.35 if hi > 0 else 1)
    ax2.ticklabel_format(useOffset=False, axis="y", style="plain")
    ax2.set_ylabel("EBIT margin (%)")
    ax2.grid(False)
    ax2.spines["right"].set_visible(True)
    ax.set_title("Revenue (bars; gold = forecast) and EBIT margin (line)", loc="left")
    return _save(fig, path)


def cash_conversion(path: Path, labels, ni, fcf, currency: str):
    plt = mpl_setup()
    import numpy as np
    fig, ax = plt.subplots(figsize=(6.6, 2.3))
    x = np.arange(len(labels))
    ax.bar(x - 0.18, [v / 1e9 for v in ni], 0.36, color=c("grey_300"), label="Net income")
    ax.bar(x + 0.18, [v / 1e9 for v in fcf], 0.36, color=c("ink"), label="Free cash flow (CFO - capex)")
    ax.set_xticks(x, labels, fontsize=7)
    ax.set_ylabel(f"{currency} bn")
    ax.set_ylim(min(0, min(ni + fcf) / 1e9 * 1.2), max(max(ni), max(fcf), 1) / 1e9 * 1.3)
    ax.legend(loc="upper left", fontsize=7, ncol=2, bbox_to_anchor=(0, 1.0))
    ax.set_title("Earnings and cash conversion (reported)", loc="left")
    return _save(fig, path)


def football_field(path: Path, bars: list[tuple[str, float, float]], price: float | None, fair: float | None, currency: str):
    plt = mpl_setup()
    fig, ax = plt.subplots(figsize=(6.6, 0.45 * len(bars) + 0.9))
    for i, (lab, lo, hi) in enumerate(bars):
        w = max(hi - lo, (max(b[2] for b in bars) or 1) * 0.006)   # keep single-point ranges visible
        ax.barh(i, w, left=lo, color=c("ink") if i else c("gold"), height=0.5)
        ax.text(hi, i, f"  {lo:,.0f}-{hi:,.0f}", va="center", fontsize=7, color=c("grey_900"))
    ax.set_yticks(range(len(bars)), [b[0] for b in bars], fontsize=7)
    ax.set_ylim(len(bars) - 0.4, -1.1)
    if price:
        ax.axvline(price, color=c("negative"), lw=1.0, ls="--")
        ax.text(price, -0.8, f" Price {price:,.0f}", color=c("negative"), fontsize=7, ha="left", va="center")
    if fair:
        ax.axvline(fair, color=c("gold"), lw=1.2)
        ax.text(fair, -0.8, f" Fair value {fair:,.0f}", color=c("gold_dark"), fontsize=7, ha="left", va="center")
    ax.set_xlabel(f"Value per share ({currency})")
    ax.grid(axis="y", visible=False)
    ax.set_title("Valuation ranges by method / scenario", loc="left")
    xmax = max(max(b[2] for b in bars), price or 0, fair or 0)
    ax.set_xlim(left=0, right=xmax * 1.22)
    return _save(fig, path)


def ev_bridge(path: Path, pv_fcff, pv_tv, fin, debt, other, currency: str):
    plt = mpl_setup()
    steps = [("PV explicit\nFCFF", pv_fcff), ("PV terminal\nvalue", pv_tv), ("+ Cash &\ninvestments", fin),
             ("- Gross\ndebt", -debt), ("- NCI /\nleases", -other)]
    fig, ax = plt.subplots(figsize=(6.6, 2.4))
    cum = 0.0
    for i, (lab, v) in enumerate(steps):
        ax.bar(i, v / 1e3, bottom=cum / 1e3, color=c("ink") if v >= 0 else c("grey_600"), width=0.6)
        cum += v
    ax.bar(len(steps), cum / 1e3, color=c("gold"), width=0.6)
    ax.set_xticks(range(len(steps) + 1), [s[0] for s in steps] + ["Equity\nvalue"], fontsize=6.5)
    ax.set_ylabel(f"{currency} bn")
    ax.set_title("Enterprise-to-equity bridge (base case)", loc="left")
    return _save(fig, path)
