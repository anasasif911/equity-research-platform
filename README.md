# eqr — autonomous institutional equity research platform

Enter a ticker; the platform resolves the security, plans the research, retrieves
and traces primary-source evidence, reconciles the financials, forecasts from
drivers, selects and runs the defensible valuation methods, has the work
independently critiqued, renders the deliverables, validates them, and writes a
manifest.

Deliverables (in `runs/<TICKER>-<timestamp>/deliverables/`):

| File | What it is |
|---|---|
| `<T>_equity_research_report.pdf` | Institutional research report (cover, thesis, business/industry, history, forecast, valuation, scenarios, catalysts, risks + impact matrix, counter-thesis, governance, evidence gaps + quality gates, methodology and source appendix) |
| `<T>_investment_summary.pdf` / `.md` | 1–2 page summary |
| `<T>_financial_model.xlsx` | Editable model with native formulas: Read Me, Dashboard, Assumptions (4 scenarios + live switch), IS, BS, CF, Schedules, WACC, DCF, Sensitivity, Scenarios, Reverse DCF, Valuation (method selection), Comps, Hist_Data, Normalization, Provenance, Sources, Discrepancies, Checks |
| `<T>_source_register.csv` / `.json` | Every source: tier, URL, publication/period, retrieval time, method, SHA-256 |
| `<T>_supporting_data.json` | Statements, drivers, assumptions, valuation, synthesis, critique, retrieval log |
| `validation_report.json` | Quality gates G01–G19, LibreOffice recalculation diff, report-vs-workbook checks, page images |
| `manifest.json`, `<T>_research_bundle.zip` | Links and hashes of all deliverables |

## Setup

```bash
pip install -e ".[dev]"
```

```bash
export EQR_SEC_USER_AGENT="Your Name your.email@example.com"
```

Optional, for unattended synthesis/critique with Claude (otherwise see "Model providers"):

```bash
export ANTHROPIC_API_KEY=...
```

System tools used for verification: LibreOffice (`soffice`, independent recalculation of the workbook) and poppler (`pdftoppm`, page images). Check what your environment can actually reach:

```bash
eqr doctor
```

## Run

Pakistan Stock Exchange (PSX) listings:

```bash
eqr run SAZEW --market psx
```

SEC-registered (US) listings:

```bash
eqr run AAPL
```

With `--market auto` (default) a ticker that is not an SEC registrant is looked up on the PSX.

Optional analyst overrides (each with rationale and evidence ids), applied within the methodology guardrails and labelled "ANALYST OVERRIDE" in the workbook:

```bash
eqr run SAZEW --market psx --assumptions my_overrides.yaml
```

With options:

```bash
eqr run AAPL --exchange NASDAQ --provider anthropic --peers MSFT,GOOGL,AMZN,META --cutoff 2026-11-02T21:00:00+00:00
```

Runs are checkpointed after every stage. If a run pauses (missing evidence, a model hand-off, a budget limit, a crash), continue it:

```bash
eqr resume runs/AAPL-20261102-160000
```

```bash
eqr status runs/AAPL-20261102-160000
```

```bash
eqr requests runs/AAPL-20261102-160000
```

Exit codes: 0 complete, 3 awaiting evidence, 4 awaiting model answer, 5 needs your input (e.g. ambiguous ticker), 6 budget stop, 1 failed.

## Markets

| Market | Financial statements | Price | Risk-free | Beta | Equity risk |
|---|---|---|---|---|---|
| PSX (`--market psx`) | Annual report (audited statements + six-year summary) and latest quarterly report PDFs from the PSX data portal, parsed by column position, OCR artefacts normalised, conflicts resolved by accounting identities | PSX company-page quote (timestamped) | Longest recent fixed-rate GoP Ijarah Sukuk cut-off yield (PSX GIS auctions) minus Pakistan default spread | Bottom-up: Damodaran EM industry beta re-levered | Damodaran mature ERP + Pakistan CRP |
| SEC (`--market us_sec`) | XBRL company facts | Yahoo/Stooq | US Treasury 10y | 60-month regression, Blume-adjusted | Damodaran implied ERP |

Allowed network hosts needed for PSX runs: `dps.psx.com.pk`, `pages.stern.nyu.edu`.

## Model providers (judgment tasks)

Code does every calculation, transformation, reconciliation and check. An inference model is still required for evidence synthesis, thesis/counter-thesis and narrative critique — keeping the methodology in version-controlled files does not remove that need.

* `anthropic` — Claude via the official SDK (`claude-opus-5-5`, adaptive thinking, JSON-schema output, server-side refusal fallback). Needs credentials.
* `agent_handoff` — the run writes `work/model_requests/<task>.json` (system prompt, evidence pack, JSON schema) and pauses; any agent (e.g. a Claude Code session) writes `work/model_responses/<task>.json`; `eqr resume` validates the answer against the schema and continues.
* `none` — deterministic fallback: quantitative work completes, qualitative sections are marked "not synthesised", gate G19 is NOT_RUN and no rating is issued.

`auto` (default) picks `anthropic` when credentials are present, otherwise `agent_handoff`.

## Evidence inbox (restricted networks)

Every retrieval that fails is logged with a deterministic file name. Anyone with access may place the exact source document at `evidence_inbox/<TICKER>/<request-id>.<ext>` (optionally with `<request-id>.meta.json` describing URL, retrieval time and method) and resume. Inbox documents are parsed and validated by the same code as direct downloads and are labelled `method: inbox` in the source register.

## Tests

```bash
python -m pytest -q
```

The suite includes an offline end-to-end run on a clearly labelled **synthetic** fixture company (`tests/fixtures/make_synthetic.py`), which can never be labelled FINAL_VALIDATED (gate G02 fails by design).

See `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md` and `docs/LIMITATIONS.md`.
