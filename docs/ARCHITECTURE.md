# Architecture

## Principle: durable intelligence outside the conversation

| Concern | Where it lives | Read by |
|---|---|---|
| Source ranking, freshness, critical inputs, rate limits | `methodology/source_policy.yaml` | `sources/evidence.py`, `quality/gates.py` |
| Accounting normalization (XBRL → canonical lines, US GAAP + IFRS), SBC/lease policy | `methodology/normalization_rules.yaml` | `extract/xbrl.py`, `model/financials.py` |
| Research plan and coverage tracking, impact-matrix fields, stopping rules | `methodology/research_plan.yaml` | `pipeline.s_plan` |
| Sector playbooks (KPIs, key questions, exposures with transmission chains, scenario overrides, archetype) | `methodology/playbooks/` | `config.Methodology.playbook_for_sic` |
| Forecasting rules, scenario definitions, expected sensitivity signs | `methodology/forecasting.yaml` | `model/assumptions.py` |
| Cost-of-capital method | `methodology/cost_of_capital.yaml` | `valuation/wacc.py`, `pipeline.model_context` |
| Valuation-method selection, archetype weights, DCF policy, target policy | `methodology/valuation_selection.yaml` | `valuation/engine.py` |
| Quality gates, tolerances, labels | `methodology/quality_gates.yaml` | `quality/gates.py` |
| Critique checklists; model prompts | `methodology/critique_checklists.yaml`, `methodology/prompts/*.md` | `analysis.py` |
| Visual identity | `brand/tokens.yaml` | `render/*` |

A fingerprint of the methodology directory is stamped into every run's manifest, so outputs are reproducible against a known methodology version.

## Workflow

```
resolve → plan → retrieve → extract → normalize → reconcile → drivers → forecast →
valuation → synthesis → critique → revise → render → validate → deliver
```

* `job.py` — run state (`runs/<id>/state.json`), atomic checkpoints in `work/*.json`, budgets (fetches, minutes, model calls, model cost), pause statuses.
* `sources/evidence.py` — evidence store: inbox → cache → network (rate-limited, retried with backoff); every byte registered with URL, tier, retrieval time, SHA-256; failed retrievals recorded as requests with inbox file names. Retrieved content is treated as untrusted data.
* `sources/sec.py`, `market.py`, `rates.py` — SEC EDGAR (ticker map, submissions, XBRL company facts, filings, 8-K earnings releases), prices (Yahoo chart API, Stooq), US Treasury par curve, Damodaran implied ERP.
* `extract/xbrl.py` — period-correct XBRL extraction (filing `fy` ≠ period; latest-filed wins; restatements logged; 52/53-week years; TTM = FY + YTD − prior YTD).
* `model/financials.py` — canonical statements, derived lines, reconciliation (BS identity, CF roll-forward, subtotals, EPS), drivers and quality flags, normalization bridge.
* `model/sheetmodel.py` + `model/build.py` — **one model definition, two evaluators**. Every forecast/valuation line is a formula template; the Python engine evaluates it for the reports and the Excel writer emits it as native formulas. LibreOffice then recalculates the workbook independently and every cell is compared (gate G12).
* `valuation/engine.py` — method selection (with reasons for rejection), scenarios, reverse DCF (bisection on the same model), margin × growth grid, comps, DDM, residual income, reconciliation, rating rule.
* `llm.py` — provider interface (Anthropic SDK / agent hand-off / none). `analysis.py` — evidence pack, synthesis schema, deterministic critics (citation existence, number audit, valuation and accounting sanity), model critique and one revision round.
* `render/` — workbook, charts, report and summary PDFs, Markdown summary, source register, manifest.
* `quality/gates.py` — gates G01–G19, LibreOffice recalculation, report-vs-workbook text checks, page images; computes FINAL_VALIDATED / DRAFT_QUALIFIED / BLOCKED_PARTIAL and re-renders the documents with the final gate table.

## Key modelling conventions

* Currency millions; SBC is an expense (no add-back in forecast CFO; share count falls only by net repurchases → no double counting).
* Forecast balance sheet in invested-capital form; `BS!check` is a genuine integrity test of all schedule links.
* DCF: valuation date = information cutoff; only cash flows after the latest balance-sheet date count (fraction row); mid-period discounting; TV = NOPAT·(1+g)·(1−g/RONIC)/(WACC−g); RONIC rule in `forecasting.yaml`.
* No circularity: interest on opening balances; WACC with current market-value weights.
* Enterprise-to-equity: + cash & investments − gross debt − NCI − lease liabilities only under IFRS 16 (US GAAP operating-lease cost is inside EBIT).
* Present intrinsic value vs 12-month target: target = value × (1 + ke) − expected dividends.
