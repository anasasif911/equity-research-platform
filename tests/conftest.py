import copy
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "synthetic"


def pytest_sessionstart(session):
    if not (FIX / "companyfacts.json").exists():
        subprocess.run([sys.executable, str(Path(__file__).parent / "fixtures" / "make_synthetic.py")], check=True)


@pytest.fixture(scope="session")
def M():
    from eqr.config import Methodology
    return Methodology()


@pytest.fixture(scope="session")
def facts():
    return json.loads((FIX / "companyfacts.json").read_text())


@pytest.fixture(scope="session")
def built(M, facts):
    from eqr.extract import xbrl
    from eqr.model import assumptions as A
    from eqr.model import financials as F
    ann = xbrl.build_annual(facts, M.normalization)
    inter = xbrl.build_interim(facts, M.normalization, ann)
    st = F.build_statements(ann, inter)
    drv = F.drivers(st)
    base = A.build_base(drv, M.forecasting, horizon=7, country="US", rf=0.042, short_rate=0.039, last_row=st["rows"][-1])
    sc = A.scenario_set(base, M.forecasting, M.playbook_for_sic(3571))
    lb = st["latest_bs"]
    market = dict(price=100.0, price_source="test", diluted_shares=xbrl.shares_outstanding(facts, M.normalization)["value"],
                  shares_source="test", debt=lb["total_debt"], fin_assets=lb["financial_assets"], nci=0.0, lease_adj=0.0,
                  lease_note="", bs_source="test", valuation_date=dt.date(2026, 11, 2), bs_date=dt.date(2026, 6, 27))
    coc = dict(rf=0.042, rf_source="t", erp=0.045, erp_source="t", beta_raw=1.1, beta_source="t", kd_pre=0.035, kd_source="t",
               tax_marginal=0.21, roic_hist=0.30)
    return {"ann": ann, "inter": inter, "st": st, "drv": drv, "base": base, "scen": sc, "market": market, "coc": coc}


@pytest.fixture
def ctx(built):
    from eqr.valuation.engine import ModelContext
    b = copy.deepcopy(built)
    return ModelContext(st=b["st"], drv=b["drv"], scenarios=b["scen"], base_assump=b["base"], market=b["market"],
                        coc=b["coc"], horizon=7)
