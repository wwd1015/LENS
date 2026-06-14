# Lending demo — end-to-end LENS run on synthetic data

A fully-worked batch run for a fictional lender ("Northwind Capital"): three
deals, 18 monthly snapshots, a hand-authored wiki, and two planted anomalies on
the final snapshot (2026-06-30):

| Planted anomaly | Detector(s) that catch it |
|---|---|
| **Sterling Mid-Market Fund II**'s senior-debt balance inflated 12% | `cross_source_wiki` (the `senior-debt-equals-pool-x-advance-rate` rule) **and** `stl_residual` — two independent families agree, so the finding gets the orchestrator's agreement confidence boost |
| Two of **Granite Peak Direct Lending**'s borrowers have a null `status` | `null_check` |

The breach is no accident: the lineage page declares the pipeline commit that
caused it (`TICKET-4821`, a hand-entered Q2 advance-rate override), so the RCA
agent traces the anomaly back to a realistic data-pipeline change — and the
brief links that commit, not LENS's own code.

## Run it

From the repo root:

```bash
lens run examples/lending_demo/lens-run.yaml
```

This detects, runs one RCA per Finding Group at/above ERROR (each RCA is a
`claude -p` headless call — make sure `claude` is on PATH, or set
`rca.enabled: false` to skip), and writes:

- `examples/lending_demo/out/findings.<run_id>.json` (+ `findings.latest.json`)
- `examples/lending_demo/out/brief.<run_id>.html` (+ `brief.latest.html`)
- `examples/lending_demo/out/rca/<run_id>/*.json`

The markdown digest prints to stdout — pasteable into Slack.

## View the brief & record feedback

```bash
lens serve --output-dir examples/lending_demo/out
# open http://127.0.0.1:8377/
```

The one-click `[real] / [false positive] / [needs more]` buttons append to
`out/feedback.jsonl`. Mark a finding **false positive** and re-run the batch:
it comes back downgraded to INFO in a collapsed "suppressed" section — never
dropped — and resurfaces automatically after `feedback.expiry_days`.

## Files

- `lens-run.yaml` — the run config (`sources`, detector suite, RCA policy,
  feedback policy, brief options)
- `data/*.csv` — synthetic data; regenerate with
  `python examples/lending_demo/generate_data.py`
- `wiki/` — hand-authored dataset / rule / lineage pages; the rule's
  structured `equation` frontmatter is what `cross_source_wiki` evaluates, and
  the lineage page's `repo_url` + `recent_changes` are what the RCA agent links
- `pipeline/models/senior_debt.sql` — stands in for the customer's data-pipeline
  producing code (the SQL with the Q2 advance-rate override); the lineage page
  points at it as `models/senior_debt.sql` in the pipeline repo
