"""Design tokens -> renderer settings. All renderers take colours, fonts and
number formats from brand/tokens.yaml only."""
from __future__ import annotations

import functools
import os

from ..config import brand

FONT_FILES = {
    "Serif": "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "Serif-Bold": "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "Sans": "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "Sans-Bold": "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "Sans-Italic": "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
}


@functools.lru_cache
def tokens() -> dict:
    return brand()


def c(name: str) -> str:
    return tokens()["color"][name]


def xl(name: str) -> str:
    return c(name).lstrip("#").upper()


@functools.lru_cache
def pdf_fonts() -> dict:
    """Register TTF fonts with reportlab; fall back to built-ins if missing."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    out = {"serif": "Times-Roman", "serif_bold": "Times-Bold", "sans": "Helvetica", "sans_bold": "Helvetica-Bold",
           "sans_italic": "Helvetica-Oblique"}
    mapping = {"serif": "Serif", "serif_bold": "Serif-Bold", "sans": "Sans", "sans_bold": "Sans-Bold", "sans_italic": "Sans-Italic"}
    for key, name in mapping.items():
        path = FONT_FILES[name]
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(f"EQR-{name}", path))
                out[key] = f"EQR-{name}"
            except Exception:  # noqa: BLE001
                pass
    return out


def mpl_setup():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    fam = "DejaVu Sans"
    for f in font_manager.fontManager.ttflist:
        if f.name == "Inter":
            fam = "Inter"
            break
    plt.rcParams.update({
        "font.family": fam, "font.size": 8, "axes.edgecolor": c("grey_300"), "axes.labelcolor": c("grey_900"),
        "axes.linewidth": 0.6, "xtick.color": c("grey_600"), "ytick.color": c("grey_600"), "axes.spines.top": False,
        "axes.spines.right": False, "axes.grid": True, "grid.color": c("grey_100"), "grid.linewidth": 0.6,
        "axes.titlesize": 9, "axes.titleweight": "bold", "axes.titlecolor": c("ink"), "legend.frameon": False,
        "figure.dpi": 200, "savefig.dpi": 200, "savefig.bbox": "tight",
    })
    return plt
