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

## Non-SEC listings

Resolution and fundamentals are automated for SEC registrants (US listings and foreign private issuers filing 10-K/20-F/40-F, US GAAP or IFRS). For other exchanges (PSX, NSE, LSE, …) an exchange adapter is required (see LIMITATIONS); until then the run stops at `needs_input` rather than analysing the wrong security.
