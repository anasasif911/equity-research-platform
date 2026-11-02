"""Cost of capital: regression beta from dated price histories, CAPM cost of
equity, cost of debt, market-value weights. Inputs carry evidence ids; any
missing sourced input is reported (never filled with an invented number)."""
from __future__ import annotations

import numpy as np

from ..sources.market import monthly_returns


def regression_beta(stock_series: list[dict], bench_series: list[dict], months: int = 60, min_months: int = 36) -> dict | None:
    rs, rb = monthly_returns(stock_series), monthly_returns(bench_series)
    common = sorted(set(rs) & set(rb))
    # drop the current (possibly partial) month
    common = common[:-1] if len(common) > min_months else common
    common = common[-months:]
    if len(common) < min_months:
        return None
    x = np.array([rb[k] for k in common])
    y = np.array([rs[k] for k in common])
    vx = np.var(x, ddof=1)
    if vx == 0:
        return None
    raw = float(np.cov(x, y, ddof=1)[0, 1] / vx)
    resid = y - (y.mean() - raw * x.mean()) - raw * x
    se = float(np.sqrt(np.sum(resid ** 2) / (len(x) - 2) / np.sum((x - x.mean()) ** 2)))
    r2 = float(np.corrcoef(x, y)[0, 1] ** 2)
    return {"raw": raw, "adjusted": 0.67 * raw + 0.33, "se": se, "r2": r2, "n": len(common),
            "start": common[0], "end": common[-1], "method": "OLS monthly returns, Blume-adjusted"}


def cost_of_capital(*, rf: float, erp: float, beta_adj: float, crp: float = 0.0, kd_pre: float,
                    tax: float, equity_value: float, debt: float) -> dict:
    ke = rf + beta_adj * erp + crp
    kd_post = kd_pre * (1 - tax)
    v = equity_value + debt
    we = equity_value / v if v else 1.0
    wacc = we * ke + (1 - we) * kd_post
    return {"ke": ke, "kd_post": kd_post, "we": we, "wd": 1 - we, "wacc": wacc}
