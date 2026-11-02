"""Autonomous workflow:
resolve -> plan -> retrieve -> extract -> normalize -> reconcile -> drivers ->
forecast -> valuation -> synthesis -> critique -> revise -> render ->
validate -> deliver.

Each stage reads checkpoints written by earlier stages (runs/<id>/work/*.json)
so a run can stop (budget, missing evidence, model hand-off, crash) and
resume exactly where it stopped."""
from __future__ import annotations

import datetime as dt
import re
import traceback
from pathlib import Path

from . import analysis as AN
from .config import Methodology, load_config
from .extract import xbrl
from .job import (AWAITING_EVIDENCE, COMPLETE, FAILED, NEEDS_INPUT, STAGES, Paused, RunState)
from .llm import make_provider
from .model import assumptions as A
from .model import financials as F
from .sources import market, rates, sec
from .sources.evidence import EvidenceStore, SourceUnavailable
from .util import log, parse_date, read_json, setup_logging, utcnow, write_json
from .valuation import engine as V
from .valuation.wacc import cost_of_capital, regression_beta


class Pipeline:
    def __init__(self, state: RunState, cfg: dict):
        self.state = state
        self.cfg = cfg
        self.M = Methodology()
        self.state.data["budget"] = cfg["budget"]
        self.store = EvidenceStore(state.run_dir, cfg, self.M.source_policy, state.data["ticker_input"], state)
        fx = state.data["options"].get("fixtures")
        if fx:
            fxd = Path(fx)
            self.store.fixtures = [
                (r"company_tickers_exchange\.json", fxd / "tickers.json"),
                (r"submissions/CIK", fxd / "submissions.json"),
                (r"companyfacts/CIK", fxd / "companyfacts.json"),
                (r"Archives/edgar/data/.*/syn-10k\.htm", fxd / "10k.htm"),
                (r"chart/SYN\b|s=syn\.us", fxd / "prices_syn.json"),
                (r"chart/SPY\b|s=spy\.us", fxd / "prices_spy.json"),
                (r"daily-treasury-rates", fxd / "treasury.csv"),
                (r"adamodar", fxd / "erp.json"),
            ]
        self.provider = make_provider(cfg, state.work, state)

    @property
    def cutoff(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.state.data["cutoff"])

    # ------------------------------------------------------------------ driver
    def run(self, until: str | None = None) -> str:
        st = self.state
        st.begin_session()
        st.data["status"] = "running"
        st.save()
        for stage in STAGES:
            if st.is_done(stage):
                continue
            log.info("== stage: %s", stage)
            st.mark(stage, "running")
            try:
                getattr(self, f"s_{stage}")()
                st.mark(stage, COMPLETE)
                st.event(stage, "complete")
            except Paused as p:
                st.mark(stage, p.status, message=p.message)
                st.data["status"] = p.status
                st.data["pause"] = {"stage": stage, "message": p.message, "detail": p.detail}
                st.save()
                log.warning("PAUSED at %s: %s", stage, p.message)
                return p.status
            except Exception as e:  # noqa: BLE001 - checkpoint any failure for resume
                st.mark(stage, FAILED, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-4000:])
                st.data["status"] = FAILED
                st.save()
                log.error("FAILED at %s: %s", stage, e)
                raise
            if until and stage == until:
                st.data["status"] = f"stopped_after_{stage}"
                st.save()
                return st.data["status"]
        st.data["status"] = COMPLETE
        st.data.pop("pause", None)
        st.save()
        return COMPLETE

    # ------------------------------------------------------------------ stages
    def s_resolve(self):
        opts = self.state.data["options"]
        res = sec.resolve(self.store, self.state.data["ticker_input"]) if not opts.get("cik") else \
            {"status": "resolved", "cik": int(opts["cik"]), "ticker": self.state.data["ticker_input"].upper(),
             "name": None, "exchange": None, "evidence_id": None}
        if res["status"] == "ambiguous":
            raise Paused(NEEDS_INPUT, res["question"], res["candidates"])
        if res["status"] == "not_found":
            raise Paused(NEEDS_INPUT,
                         f"'{self.state.data['ticker_input']}' is not an SEC-registered ticker. This version automates "
                         "SEC filers (US listings and foreign private issuers filing 10-K/20-F/40-F). For other exchanges, "
                         "supply the issuer's annual reports via the evidence inbox and an explicit --cik-like identifier "
                         "(see docs/OPERATIONS.md, 'Non-SEC listings').", res)
        subs, sub_ev = sec.submissions(self.store, res["cik"])
        addr = (subs.get("addresses") or {}).get("business") or {}
        foreign = str(addr.get("isForeignLocation") or "0") in ("1", "True", "true")
        country = (addr.get("stateOrCountryDescription") or "DEFAULT") if foreign else "US"
        sec_info = {**res, "name": res.get("name") or subs.get("name"), "sic": int(subs["sic"]) if subs.get("sic") else None,
                    "sic_description": subs.get("sicDescription"), "fiscal_year_end": subs.get("fiscalYearEnd"),
                    "state_of_incorporation": subs.get("stateOfIncorporation"), "tickers": subs.get("tickers"),
                    "exchanges": subs.get("exchanges"), "country": country, "submissions_evidence_id": sub_ev,
                    "ticker": res.get("ticker") or (subs.get("tickers") or [None])[0]}
        if sec_info["exchange"] is None and subs.get("exchanges"):
            sec_info["exchange"] = subs["exchanges"][0]
        self.state.put("security", sec_info)
        self.state.put("submissions_meta", {"recent": sec.recent_filings(subs)[:200]})

    def s_plan(self):
        sec_info = self.state.get("security")
        pb = self.M.playbook_for_sic(sec_info.get("sic"), self.state.data["options"].get("sector"))
        rp = self.M.research_plan
        items = []
        for cat, lst in rp["coverage"].items():
            for it in lst:
                items.append({**it, "category": cat, "status": "open", "evidence_ids": []})
        for q in pb.get("key_questions", []):
            items.append({"id": f"S{len(items)}", "question": q, "category": "sector", "priority": "high",
                          "evidence": ["filing_text"], "status": "open", "evidence_ids": []})
        horizon = self.M.forecasting["horizon"]["by_archetype"].get(pb["archetype"], self.M.forecasting["horizon"]["default_years"])
        self.state.put("plan", {"playbook": pb, "items": items, "horizon": horizon,
                                "methodology": self.M.version_fingerprint(), "created_at": utcnow().isoformat()})

    def s_retrieve(self):
        sec_info = self.state.get("security")
        cik = sec_info["cik"]
        out: dict = {"blocked": []}
        facts, fev = sec.companyfacts(self.store, cik)  # critical: raises if unavailable
        write_json(self.state.work / "companyfacts.json", facts)
        out["companyfacts_evidence_id"] = fev
        subs = {"filings": {"recent": {}}}
        recent = self.state.get("submissions_meta")["recent"]
        # narrative documents (non-critical; gaps recorded)
        excerpts = []
        tenk = next((f for f in recent if f["form"] in ("10-K", "20-F", "40-F")), None)
        if tenk:
            try:
                text, dev = sec.filing_document(self.store, cik, tenk)
                for name, body in sec.extract_sections(text).items():
                    excerpts.append(self._excerpt(self.store.get(dev), f"{tenk['form']} ({tenk['reportDate']}) - {name.replace('_', ' ')}", body,
                                                  limit={"business": 15000, "risk_factors": 24000, "mdna": 24000}.get(name, 6000)))
            except SourceUnavailable as e:
                out["blocked"].append(e.request_id)
        last_periodic = next((f for f in recent if f["form"] in ("10-K", "10-Q", "20-F", "40-F")), None)
        if last_periodic:
            subs["filings"]["recent"] = {k: [f.get(k) for f in recent] for k in ("accessionNumber", "filingDate", "reportDate",
                                                                                  "form", "primaryDocument", "items")}
            er = sec.earnings_release_after(self.store, cik, subs, last_periodic["filingDate"])
            if er:
                text, eid, f = er
                excerpts.append(self._excerpt(self.store.get(eid), f"Earnings release filed {f['filingDate']} (8-K Item 2.02)",
                                              text, limit=16000))
                out["subsequent_results"] = {"filed": f["filingDate"], "evidence_id": eid,
                                             "note": "Results announced after the latest periodic XBRL filing; used qualitatively, "
                                                     "not in the reconciled historical model."}
        self.state.put("excerpts", excerpts)
        # market inputs (critical for valuation)
        cutoff = self.cutoff.date()
        mi: dict = {}
        tk = sec_info["ticker"]
        px = market.price_history(self.store, tk, monthly=False, critical=True, label="Reference")
        if px:
            parsed, pev = px
            mi["price"] = {"value": parsed["price"], "time": parsed["price_time"], "currency": parsed["currency"],
                           "evidence_id": pev, "source": self.store.get(pev).publisher}
        stock_m = market.price_history(self.store, tk, monthly=True, critical=True, label="Beta")
        bench_m = market.price_history(self.store, "SPY", monthly=True, critical=True, label="Benchmark (S&P 500 ETF)")
        if stock_m and bench_m:
            b = regression_beta(stock_m[0]["series"], bench_m[0]["series"])
            if b:
                mi["beta"] = {"value": b["raw"], "adjusted": b["adjusted"], "r2": b["r2"], "se": b["se"], "n": b["n"],
                              "window": f"{b['start']} to {b['end']}", "method": b["method"],
                              "evidence_ids": [stock_m[1], bench_m[1]]}
        rf = rates.risk_free_usd(self.store, cutoff)
        if rf:
            mi["rf"] = {"value": rf["value"], "as_of": rf["as_of"], "evidence_id": rf["evidence_id"], "tenor": rf["tenor"],
                        "short_rate": rf["curve"].get("1 Yr")}
        erp = rates.equity_risk_premium(self.store)
        if erp:
            mi["erp"] = {"value": erp["value"], "as_of": erp.get("as_of"), "evidence_id": erp["evidence_id"],
                         "context": erp.get("context", "")[:300]}
        self.state.put("market_raw", mi)
        # peers (non-critical)
        out["peers"] = self._retrieve_peers(sec_info)
        self.state.put("retrieved", out)
        missing = [k for k in ("price", "rf", "erp", "beta") if k not in mi]
        if missing:
            blocked = self.store.blocked_requests(critical_only=True)
            raise Paused(AWAITING_EVIDENCE,
                         f"Critical market inputs unavailable: {', '.join(missing)}. Allow the hosts in the environment's "
                         f"network policy or place the source documents in the evidence inbox, then `eqr resume`.",
                         {"missing": missing, "requests": blocked})

    def _excerpt(self, parent_ev, title: str, body: str, limit: int) -> dict:
        d = self.state.run_dir / "sources" / "excerpts"
        d.mkdir(parents=True, exist_ok=True)
        text = body[:limit]
        safe = re.sub(r"[^A-Za-z0-9]+", "_", title)[:60]
        p = d / f"{safe}.txt"
        p.write_text(text)
        ev = self.store.add(kind="document_excerpt", title=title, publisher=parent_ev.publisher, url=parent_ev.url,
                            tier=parent_ev.tier, retrieved_at=parent_ev.retrieved_at, method="derived",
                            sha256=None, local_path=str(p.relative_to(self.state.run_dir)), published_at=parent_ev.published_at,
                            notes=f"Excerpt (first {len(text):,} of {len(body):,} chars) of {parent_ev.id}",
                            synthetic=parent_ev.synthetic)
        return {"evidence_id": ev.id, "title": title, "text": text, "truncated": len(body) > limit}

    def _retrieve_peers(self, sec_info) -> list[dict]:
        pb = self.state.get("plan")["playbook"]
        peers_cfg = self.state.data["options"].get("peers") or pb.get("peers_hint") or []
        peers = [p for p in peers_cfg if p.upper() != (sec_info["ticker"] or "").upper()][: self.cfg["budget"]["max_peers"]]
        out = []
        for p in peers:
            try:
                r = sec.resolve(self.store, p)
                if r["status"] != "resolved":
                    continue
                facts, fev = sec.companyfacts(self.store, r["cik"])
                ann = xbrl.build_annual(facts, self.M.normalization, max_years=3)
                inter = xbrl.build_interim(facts, self.M.normalization, ann)
                st = F.build_statements(ann, inter)
                if ann["currency"] != "USD":
                    continue  # cross-currency comps need FX conversion: excluded (disclosed)
                ttm = st["ttm"] or st["rows"][-1]
                bs = st["latest_bs"] or st["rows"][-1]
                so = xbrl.shares_outstanding(facts, self.M.normalization)
                px = market.price_history(self.store, p, monthly=False, critical=False, label="Peer")
                if not (px and so and ttm.get("revenue")):
                    continue
                out.append({"ticker": p, "name": r["name"], "price": px[0]["price"], "price_time": px[0]["price_time"],
                            "shares": so["value"], "fin_assets": bs.get("financial_assets") or 0, "debt": bs.get("total_debt") or 0,
                            "nci": bs.get("nci") or 0, "ttm_revenue": ttm.get("revenue"), "ttm_ebitda": ttm.get("ebitda"),
                            "ttm_ebit": ttm.get("operating_income"), "ttm_net_income": ttm.get("net_income"),
                            "ttm_basis": "TTM" if st["ttm"] else "last fiscal year", "evidence_ids": [fev, px[1]]})
            except (SourceUnavailable, KeyError, ValueError, TypeError) as e:
                log.info("peer %s skipped: %s", p, e)
        return out

    def s_extract(self):
        facts = read_json(self.state.work / "companyfacts.json")
        rules = self.M.normalization
        ann = xbrl.build_annual(facts, rules)
        inter = xbrl.build_interim(facts, rules, ann)
        so = xbrl.shares_outstanding(facts, rules)
        dps = xbrl.dividends_per_share(facts, ann["taxonomy"], ann["currency"], ann["periods"], rules["periods"]["annual_forms"])
        # filing-level references for traceability (one register row per filing used)
        accns = {}
        for item, d in ann["prov"].items():
            for end, pv in d.items():
                if pv and pv.get("accn"):
                    accns.setdefault(pv["accn"], pv)
        for k, pv in (inter or {}).get("prov", {}).items():
            if pv and pv.get("accn"):
                accns.setdefault(pv["accn"], pv)
        cik = self.state.get("security")["cik"]
        fev = self.state.get("retrieved")["companyfacts_evidence_id"]
        synthetic = self.store.get(fev).synthetic
        filing_ev = {}
        for accn, pv in sorted(accns.items(), key=lambda kv: kv[1].get("filed") or ""):
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/"
            ev = self.store.add(kind="sec_filing_ref", title=f"{pv.get('form')} filed {pv.get('filed')} (accession {accn})",
                                publisher="U.S. SEC / issuer", url=url, tier=1, retrieved_at=self.store.get(fev).retrieved_at,
                                method="derived", published_at=pv.get("filed"), notes=f"XBRL values obtained via {fev}",
                                synthetic=synthetic)
            filing_ev[accn] = ev.id
        self.state.put("extracted", {"annual": ann, "interim": inter, "shares_outstanding": so, "dps": dps,
                                     "filing_evidence": filing_ev, "extension_namespaces": xbrl.segment_like_facts(facts, ann["taxonomy"])})

    def s_normalize(self):
        ex = self.state.get("extracted")
        st = F.build_statements(ex["annual"], ex["interim"])
        facts = read_json(self.state.work / "companyfacts.json")
        bridge = F.normalization_bridge(facts, st, self.M.normalization)
        self.state.put("statements", st)
        self.state.put("normalization_bridge", bridge)

    def s_reconcile(self):
        st = self.state.get("statements")
        ex = self.state.get("extracted")
        rec = F.reconcile(st, self.M.gates["tolerances"])
        for r in ex["annual"]["restatements"]:
            rec["discrepancies"].append({"type": "restatement", "period": r["period_end"], "item": r["item"],
                                         "diff": r["restated"] - r["original"],
                                         "resolution": f"Latest-filed value used ({r['restated_accn']}); original {r['original']:,.0f} "
                                                       f"in {r['original_accn']} retained in log."})
        mi = self.state.get("market_raw") or {}
        self.state.put("reconciliation", rec)

    def s_drivers(self):
        st = self.state.get("statements")
        self.state.put("drivers", F.drivers(st))

    def s_forecast(self):
        st, drv = self.state.get("statements"), self.state.get("drivers")
        plan, mi, sec_info = self.state.get("plan"), self.state.get("market_raw"), self.state.get("security")
        country = sec_info.get("country") if sec_info.get("country") in ("US", "PK", "IN", "GB") else "DEFAULT"
        base = A.build_base(drv, self.M.forecasting, horizon=plan["horizon"], country=country,
                            rf=mi["rf"]["value"], short_rate=mi["rf"].get("short_rate"), last_row=st["rows"][-1])
        scen = A.scenario_set(base, self.M.forecasting, plan["playbook"])
        self.state.put("assumptions", base)
        self.state.put("scenarios", scen)

    # -------------------------------------------------------------- valuation
    def model_context(self) -> V.ModelContext:
        st, drv = self.state.get("statements"), self.state.get("drivers")
        base, scen = self.state.get("assumptions"), self.state.get("scenarios")
        mi, ex = self.state.get("market_raw"), self.state.get("extracted")
        plan = self.state.get("plan")
        bs = st["latest_bs"] or st["rows"][-1]
        bs_date = parse_date(st["interim_end"]) if st.get("latest_bs") else parse_date(st["periods"][-1]["end"])
        so = ex["shares_outstanding"]
        last = st["rows"][-1]
        ytd = (st.get("ytd") or {})
        dil_w, bas_w = last.get("shares_diluted_weighted"), last.get("shares_basic_weighted")
        dilution = max(0.0, (dil_w or 0) - (bas_w or 0))
        shares = (so["value"] if so else dil_w) + (dilution if so else 0)
        fev = self.state.get("retrieved")["companyfacts_evidence_id"]
        ifrs = st["taxonomy"] == "ifrs-full"
        lease = bs.get("operating_lease_liabilities") or 0
        market_kw = {
            "price": mi["price"]["value"], "price_source": f"{mi['price']['source']} {mi['price']['time']} [{mi['price']['evidence_id']}]",
            "diluted_shares": shares,
            "shares_source": (f"Cover-page shares outstanding {so['value']:,.0f} as of {so['as_of']} ({so['accn']}) + dilutive "
                              f"securities {dilution:,.0f} (FY diluted - basic weighted) [{fev}]") if so else f"FY diluted weighted [{fev}]",
            "debt": bs.get("total_debt") or 0, "fin_assets": bs.get("financial_assets") or 0, "nci": bs.get("nci") or 0,
            "lease_adj": lease if ifrs else 0.0,
            "lease_note": ("IFRS 16: lease liabilities treated as debt" if ifrs else
                           f"US GAAP operating leases ({lease / 1e6:,.0f}m) are operating items (cost in EBIT): not deducted"),
            "bs_source": f"Balance sheet as of {bs_date.isoformat()} [{fev}]",
            "valuation_date": self.cutoff.date(), "bs_date": bs_date,
        }
        roic = drv["summary"]["roic"]["avg3"]
        coc = {"rf": mi["rf"]["value"], "rf_source": f"US Treasury 10y par yield {mi['rf']['as_of']} [{mi['rf']['evidence_id']}]",
               "erp": mi["erp"]["value"], "erp_source": f"Damodaran implied ERP {mi['erp'].get('as_of') or ''} [{mi['erp']['evidence_id']}]",
               "beta_raw": mi["beta"]["value"],
               "beta_source": f"{mi['beta']['method']}, {mi['beta']['n']} months {mi['beta']['window']}, R2 {mi['beta']['r2']:.2f} "
                              f"[{', '.join(mi['beta']['evidence_ids'])}]",
               "kd_pre": base["drivers"]["kd"]["values"][0], "kd_source": base["drivers"]["kd"]["rationale"],
               "tax_marginal": base["statutory_tax"], "roic_hist": roic if roic is not None else 0.0}
        return V.ModelContext(st=st, drv=drv, scenarios=scen, base_assump=base, market=market_kw, coc=coc,
                              horizon=plan["horizon"])

    def s_valuation(self):
        ctx = self.model_context()
        st, drv, mi = self.state.get("statements"), self.state.get("drivers"), self.state.get("market_raw")
        plan, ex = self.state.get("plan"), self.state.get("extracted")
        res = ctx.evaluate(1)
        if res["errors"]:
            raise RuntimeError(f"model evaluation errors: {res['errors'][:5]}")
        S = res["scalars"]
        scen = V.scenario_results(ctx)
        rd = V.reverse_dcf(ctx, mi["price"]["value"])
        mg = V.margin_growth_grid(ctx)
        sens = {"wacc": [S["wacc"] + (i - 2) * 0.005 for i in range(5)], "g": [S["terminal_g"] + (j - 2) * 0.005 for j in range(5)],
                "values": [[S[f"sens_{i}_{j}"] for j in range(5)] for i in range(5)]}
        rows = st["rows"]
        payouts = [r.get("dividends_paid") for r in rows[-5:]]
        payout = drv["summary"]["payout_ratio"]["avg3"]
        peers = self.state.get("retrieved").get("peers") or []
        avail = {"historical_financials": True, "market_price": True, "risk_free_rate": True, "equity_risk_premium": True,
                 "beta": True, "dividend_history": all(p for p in payouts) and len(payouts) >= 5,
                 "payout_ok": payout is not None and 0.3 <= payout <= 1.0,
                 "peer_set_with_market_data": len(peers) >= 3, "book_value": rows[-1].get("total_equity") is not None,
                 "roe_history": True, "segment_profit_disclosure": False}
        methods = V.select_methods(self.M.valuation, plan["playbook"]["archetype"], avail, plan["playbook"])
        values = {"fcff_dcf": S["value_per_share"]}
        comps_summary = None
        mk = ctx.kw["market"]
        if any(m["id"] == "trading_comps" and m["status"] == "selected" for m in methods):
            ttm = st["ttm"] or rows[-1]
            target = {"ticker": self.state.get("security")["ticker"], "price": mk["price"], "shares": mk["diluted_shares"],
                      "fin_assets": mk["fin_assets"], "debt": mk["debt"], "nci": mk["nci"], "ttm_revenue": ttm.get("revenue"),
                      "ttm_ebitda": ttm.get("ebitda"), "ttm_net_income": ttm.get("net_income")}
            c = V.comps(target, peers)
            comps_summary = c
            if c["central"] and c["n_valid"] >= 3:
                values["trading_comps"] = c["central"]
            else:
                for m in methods:
                    if m["id"] == "trading_comps":
                        m.update(status="rejected", reason="fewer than 3 peers with valid multiples", weight=0.0)
        if any(m["id"] == "ddm" and m["status"] == "selected" for m in methods):
            dps = res["rows"][("IS", "dps")][-ctx.kw["horizon"]:]
            d = V.ddm(dps, S["ke"], S["terminal_g"])
            if d:
                values["ddm"] = d["value"]
        if any(m["id"] == "residual_income" and m["status"] == "selected" for m in methods):
            bv = (st["latest_bs"] or rows[-1]).get("total_equity")
            roe = drv["summary"]["roe"]["avg3"]
            if bv and roe is not None:
                ri = V.residual_income(bv, roe, S["ke"], S["terminal_g"], payout or 0.3, ctx.kw["horizon"], mk["diluted_shares"])
                values["residual_income"] = ri["value_per_share"]
        # renormalise weights over methods that produced values
        sel = [m for m in methods if m["status"] == "selected" and m["id"] in values]
        tot = sum(m["weight"] for m in sel) or 1
        for m in methods:
            m["weight"] = (m["weight"] / tot) if m in sel else 0.0
        rec = V.reconcile_values(methods, values, scen, mk["price"], S["ke"], S["dps_12m"], self.M.valuation)
        dcf_base = {k: S[k] for k in ("sum_pv", "tv", "pv_tv", "ev", "tv_share", "equity_value", "value_per_share", "upside",
                                      "wacc", "ke", "terminal_g", "ronic", "implied_tv_ebitda", "implied_tv_pe", "target_12m",
                                      "dps_12m", "exp_total_return", "fin_assets_latest", "debt_latest", "nci_latest",
                                      "lease_adj", "diluted_shares", "kd_post", "we", "beta_adj")}
        self.state.put("valuation", {"methods": methods, "values": values, "dcf_base": dcf_base, "scenarios": scen,
                                     "reverse_dcf": rd, "sensitivity": sens, "margin_growth_grid": mg,
                                     "reconciled": rec, "comps_summary": comps_summary,
                                     "checks": {k: S[k] for k in S if k.startswith("chk_")}})

    # -------------------------------------------------------------- synthesis / critique
    def _ctx(self) -> dict:
        mi = self.state.get("market_raw")
        return {"security": self.state.get("security"), "statements": self.state.get("statements"),
                "drivers": self.state.get("drivers"), "reconciliation": self.state.get("reconciliation"),
                "assumptions": self.state.get("assumptions"), "valuation": self.state.get("valuation"),
                "playbook": self.state.get("plan")["playbook"], "plan": self.state.get("plan")["items"],
                "excerpts": self.state.get("excerpts") or [],
                "market_inputs": {"price": mi["price"], "rf": mi["rf"], "erp": mi["erp"], "beta": mi.get("beta")},
                "evidence_ids": {"financials": self.state.get("retrieved")["companyfacts_evidence_id"]}}

    def s_synthesis(self):
        ctx = self._ctx()
        pack = AN.evidence_pack(ctx)
        write_json(self.state.work / "evidence_pack.json", pack)
        out = self.provider.complete("synthesis", self.M.prompt("synthesis"), AN.build_prompt(pack), AN.SYNTHESIS_SCHEMA)
        if out is None:
            out = AN.deterministic_synthesis(ctx)
            out["_provider"] = "none"
        else:
            out["_provider"] = self.provider.name
        self.state.put("synthesis", out)

    def s_critique(self):
        ctx = self._ctx()
        synth = self.state.get("synthesis")
        pack = read_json(self.state.work / "evidence_pack.json")
        ids = set(self.store.items)
        det = AN.critique_model_and_valuation(ctx) + AN.critique_numbers_and_citations(synth, pack, ids)
        model_f = []
        if synth.get("_provider") not in ("none", None):
            prompt = AN.build_prompt(pack) + "\n<draft>\n" + __import__("json").dumps(synth) + "\n</draft>\n" + \
                "Deterministic critique findings:\n" + __import__("json").dumps(det)
            r = self.provider.complete("critique", self.M.prompt("critique"), prompt, AN.CRITIQUE_SCHEMA)
            model_f = (r or {}).get("findings", [])
        self.state.put("critique", {"deterministic": det, "model": model_f, "model_ran": synth.get("_provider") not in ("none", None)})

    def s_revise(self):
        crit = self.state.get("critique")
        synth = self.state.get("synthesis")
        blocking = [f for f in crit["deterministic"] + crit["model"] if f["severity"] in ("critical", "major")
                    and f["area"] in ("thesis", "citation")]
        log_entries = []
        if blocking and synth.get("_provider") not in ("none", None):
            ctx = self._ctx()
            pack = read_json(self.state.work / "evidence_pack.json")
            r = self.provider.complete("synthesis_revised", self.M.prompt("synthesis"), AN.build_prompt(pack, blocking),
                                       AN.SYNTHESIS_SCHEMA)
            if r:
                r["_provider"] = synth["_provider"]
                r["_revision"] = 1
                self.state.put("synthesis_draft1", synth)
                self.state.put("synthesis", r)
                after = AN.critique_numbers_and_citations(r, pack, set(self.store.items))
                log_entries.append({"round": 1, "findings_addressed": len(blocking),
                                    "remaining_citation_findings": [f for f in after if f["severity"] != "minor"]})
                crit["post_revision"] = after
                self.state.put("critique", crit)
        self.state.put("revisions", log_entries)

    # -------------------------------------------------------------- render / validate / deliver
    def s_render(self):
        from .render import render_all
        render_all(self, final=False)

    def s_validate(self):
        from .quality.gates import run_validation
        run_validation(self)

    def s_deliver(self):
        from .render.manifest import write_manifest
        write_manifest(self)


def start(ticker: str, *, config: str | None = None, options: dict | None = None, verbose: bool = False) -> RunState:
    cfg = load_config(config)
    st = RunState.create(Path(cfg["runs_dir"]), ticker, options or {})
    setup_logging(verbose, st.run_dir / "run.log")
    return st


def run(state: RunState, config: str | None = None, until: str | None = None, overrides: dict | None = None) -> str:
    cfg = load_config(config, overrides or state.data["options"].get("config_overrides"))
    return Pipeline(state, cfg).run(until)
