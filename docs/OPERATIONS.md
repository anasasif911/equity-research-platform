# Operations: what unattended running requires

Nothing here claims the platform is deployed or running persistently. This
repository runs wherever you run it; a cloud coding session is ephemeral and is
not a host.

## Required for a fully autonomous run

1. **Outbound HTTPS** to:
   * `www.sec.gov`, `data.sec.gov` (ticker map, submissions, XBRL facts, filings) — send a real `EQR_SEC_USER_AGENT`; SEC fair-access limit is 10 req/s, the platform uses 5.
   * `query1.finance.yahoo.com` and/or `stooq.com` (prices for reference price, beta and peers).
   * `home.treasury.gov` (risk-free curve).
   * `pages.stern.nyu.edu` (implied ERP).
   * `api.anthropic.com` (if `provider: anthropic`).
   Run `eqr doctor` to see which are reachable.
2. **Model credentials** for unattended judgment tasks (`ANTHROPIC_API_KEY` or another SDK credential source). Without them the run pauses at each model task (`agent_handoff`) or completes without qualitative synthesis (`none`).
3. **LibreOffice + poppler** on the host for workbook recalculation and page images. Without LibreOffice, gate G12 is NOT_RUN and the label cannot be FINAL_VALIDATED.
4. **Persistent storage** for `runs/`, `.cache/` and `evidence_inbox/` (a volume or object store sync) so runs can resume after a restart.
5. **A scheduler/worker** if runs should start without a person: e.g. a container with the above, triggered by cron, a queue worker, or a CI job (`eqr run <TICKER>`; on exit code 3/4, alert and `eqr resume` later).

## Budgets and stopping

`config/default.yaml` → `budget` (max fetches, minutes, model calls, model cost, peers). Exceeding any budget stops the run with a checkpoint (`budget_stop`, exit 6); raise the budget and resume.

## Pause states and how to clear them

| Status | Meaning | Action |
|---|---|---|
| `needs_input` | Ambiguous or unknown ticker | Re-run with `TICKER:EXCHANGE` or `--cik` |
| `awaiting_evidence` | A critical source could not be retrieved | Allow the host, or place the document in the evidence inbox (`eqr requests <run>` lists exact file names), then `eqr resume` |
| `awaiting_model` | Model task handed off | Answer `work/model_responses/<task>.json` (schema in the request), then `eqr resume` |
| `budget_stop` | Budget exhausted | Raise budget, `eqr resume` |
| `failed` | Exception (trace in `state.json`) | Fix, `eqr resume` (re-enters the failed stage) |

## Visual review record

Gate G17 is QUALIFIED after automated layout checks (tables within frame, summary ≤ 2 pages, page images rendered). It becomes VERIFIED when a reviewer (human or agent) inspects `validation/png/*.png` and writes `work/visual_review.json`:

```json
{"reviewer": "name", "verdict": "pass", "notes": "pages inspected ..."}
```

then `eqr resume <run> --from-stage validate`.

## PSX listings

Automated end to end for companies whose annual report on the PSX data portal is a text (or OCR-text) PDF:

* hosts: `dps.psx.com.pk` (company page, filings, GIS auctions), `pages.stern.nyu.edu` (Damodaran country risk, ERP, EM betas);
* PSX's JSON/AJAX endpoints refuse non-browser clients (403); the platform does not bypass this, so price history is not used
  (beta is bottom-up, which is preferable for thinly traded PSX stocks anyway);
* scanned image-only PDFs need OCR (not installed here); such runs stop at `awaiting_evidence` with an explanation;
* sector playbooks are mapped from the PSX sector name (`methodology/markets/psx.yaml`); `autos_pk` is detailed, other sectors
  currently fall back to general playbooks.

Judgment overrides: write `work/assumption_overrides.json` in the run folder (or pass `--assumptions`) and resume from `forecast`.
