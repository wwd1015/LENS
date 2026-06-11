# Lending demo — end-to-end LENS run on synthetic data

A fully-worked batch run: three deals, 18 monthly snapshots, a hand-authored
wiki, and two planted anomalies on the final snapshot (2026-06-30):

| Planted anomaly | Detector(s) that catch it |
|---|---|
| Deal **D2**'s senior-debt balance inflated 12% | `cross_source_wiki` (the `senior-debt-equals-pool-x-advance-rate` rule) **and** `stl_residual` — two independent families agree, so the finding gets the orchestrator's agreement confidence boost |
| Two of deal **D3**'s loans have a null `status` | `null_check` |

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
  structured `equation` frontmatter is what `cross_source_wiki` evaluates
- `transforms/build_senior_debt.sql` — toy producing code the lineage page
  points at, so the RCA agent has a real path to `git log`
