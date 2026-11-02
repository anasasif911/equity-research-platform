# Known limitations (precise)

## Data coverage
* **Automated fundamentals: SEC XBRL filers only.** Other exchanges need an adapter that maps exchange filings/annual reports to the canonical schema (the normalization layer already handles IFRS concepts). Not built.
* **Segment data:** the SEC company-facts API excludes dimensional facts, so segment revenue/profit is not in the structured model; segment discussion comes only from filing text via the inference model. SOTP is therefore rejected.
* **Results announced after the latest periodic filing** (8-K earnings releases) are retrieved and used qualitatively, not merged into the reconciled history.
* **Consensus estimates, short interest, options, ownership:** no free, lawful, dated source is wired in; these are reported as evidence gaps, never estimated.
* **Peers:** comps use USD SEC filers only (no FX conversion); peers come from the sector playbook or `--peers`.
* **Prices** come from aggregators (tier 4) with timestamps; a second-source cross-check is recorded only when both sources are reachable.

## Methods
* Implemented: FCFF DCF, reverse DCF, trading comps, DDM, residual income. Not implemented: NAV/resource NAV, FFO/AFFO, rNPV, liquidation, precedent transactions, SOTP. The selector rejects them with that reason; for archetypes that need them (REITs, resource, holding companies, pre-revenue, distressed) the output states that no specialist valuation was produced, and FCFF is labelled a cross-check only.
* Banks/insurers: FCFF is rejected; residual income/DDM are computed in the engine but the Excel workbook is still the industrial template (a bank-specific workbook is not built).
* Forecast balance sheet is condensed (invested-capital form); other operating assets/liabilities are held flat.
* Country risk premium: zero for US filers; non-US requires a sourced CRP via the inbox.

## Verification
* Workbook recalculation is verified with LibreOffice, not Microsoft Excel. Formulas used are standard (`CHOOSE`, `SUM`, `MAX`, `MIN`, `ABS`, `IF`, `MEDIAN`, `COUNTIF`) and behave identically in Excel, but Excel itself was not run.
* Visual inspection is automated (layout audit + page images) plus a reviewer record; the platform cannot judge aesthetics by itself.

## Model
* Qualitative synthesis needs an inference model. Without one, the run states so and withholds a rating.

## Brand
* No existing brand assets were found; `brand/tokens.yaml` is a recorded fallback (black / white / muted gold). Replace the values to apply the true house style.
